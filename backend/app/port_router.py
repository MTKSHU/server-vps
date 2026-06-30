import asyncio
import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


BACKEND_URL = os.environ.get("ROUTER_BACKEND_URL", "http://127.0.0.1:80").rstrip("/")
ROUTER_TOKEN = os.environ.get("PORT_ROUTER_TOKEN", "")
SYNC_INTERVAL = float(os.environ.get("PORT_ROUTER_SYNC_INTERVAL", "3"))
CONNECT_TIMEOUT = float(os.environ.get("PORT_ROUTER_CONNECT_TIMEOUT", "8"))


@dataclass(frozen=True)
class Route:
    protocol: str
    public_port: int
    node_ip: str
    node_port: int
    container_name: str
    container_port: int


class TCPListener:
    def __init__(self, route: Route):
        self.route = route
        self.server: asyncio.AbstractServer | None = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "0.0.0.0", self.route.public_port)
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} tcp :{self.route.public_port} -> "
            f"{self.route.node_ip}:{self.route.node_port}",
            flush=True,
        )

    async def close(self):
        if not self.server:
            return
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(self.route.node_ip, self.route.node_port),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception:
            client_writer.close()
            await client_writer.wait_closed()
            return

        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        await asyncio.gather(pipe(client_reader, target_writer), pipe(target_reader, client_writer))


class UDPProxy(asyncio.DatagramProtocol):
    def __init__(self, route: Route):
        self.route = route
        self.transport: asyncio.DatagramTransport | None = None
        self.clients: dict[asyncio.DatagramTransport, tuple[str, int]] = {}

    def connection_made(self, transport: asyncio.BaseTransport):
        self.transport = transport  # type: ignore[assignment]
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} udp :{self.route.public_port} -> "
            f"{self.route.node_ip}:{self.route.node_port}",
            flush=True,
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        if not self.transport:
            return
        loop = asyncio.get_running_loop()
        loop.create_task(self.forward(data, addr))

    async def close(self):
        if self.transport:
            self.transport.close()

    async def forward(self, data: bytes, addr: tuple[str, int]):
        loop = asyncio.get_running_loop()
        response = UDPResponse(self.transport, addr) if self.transport else None
        if not response:
            return
        target_transport, _ = await loop.create_datagram_endpoint(
            lambda: response,
            remote_addr=(self.route.node_ip, self.route.node_port),
        )
        try:
            target_transport.sendto(data)
            await asyncio.sleep(CONNECT_TIMEOUT)
        finally:
            target_transport.close()


class UDPResponse(asyncio.DatagramProtocol):
    def __init__(self, public_transport: asyncio.DatagramTransport, client_addr: tuple[str, int]):
        self.public_transport = public_transport
        self.client_addr = client_addr

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        self.public_transport.sendto(data, self.client_addr)


async def fetch_routes() -> dict[tuple[str, int], Route]:
    def load() -> dict[str, Any]:
        request = urllib.request.Request(f"{BACKEND_URL}/api/internal/port-routes")
        if ROUTER_TOKEN:
            request.add_header("X-Port-Router-Token", ROUTER_TOKEN)
        with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    data = await asyncio.to_thread(load)
    routes: dict[tuple[str, int], Route] = {}
    for item in data.get("routes", []):
        protocol = str(item["protocol"]).lower()
        public_port = int(item["host_port"])
        node_port = int(item["node_port"])
        node_ip = str(item["node_ip"])
        if protocol not in {"tcp", "udp"} or public_port <= 0 or node_port <= 0 or not node_ip:
            continue
        routes[(protocol, public_port)] = Route(
            protocol=protocol,
            public_port=public_port,
            node_ip=node_ip,
            node_port=node_port,
            container_name=str(item["container_name"]),
            container_port=int(item["container_port"]),
        )
    return routes


async def main():
    listeners: dict[tuple[str, int], Any] = {}
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        try:
            routes = await fetch_routes()
            for key, listener in list(listeners.items()):
                route = routes.get(key)
                if route == getattr(listener, "route", None):
                    continue
                await listener.close()
                listeners.pop(key, None)
            for key, route in routes.items():
                if key in listeners:
                    continue
                try:
                    if route.protocol == "tcp":
                        listener = TCPListener(route)
                        await listener.start()
                    else:
                        _, protocol = await loop.create_datagram_endpoint(
                            lambda route=route: UDPProxy(route),
                            local_addr=("0.0.0.0", route.public_port),
                        )
                        listener = protocol
                    listeners[key] = listener
                except OSError as exc:
                    print(f"cannot listen {route.protocol} :{route.public_port}: {exc}", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"route sync failed: {exc}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SYNC_INTERVAL)
        except asyncio.TimeoutError:
            pass

    for listener in list(listeners.values()):
        result = listener.close()
        if asyncio.iscoroutine(result):
            await result


if __name__ == "__main__":
    if hasattr(socket, "SO_REUSEADDR"):
        asyncio.run(main())
