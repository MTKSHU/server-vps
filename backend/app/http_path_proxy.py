"""
http_path_proxy.py — 基于路径的 HTTP 反向代理

将 /c/<container-name>/<port-name>/... 的请求路由到对应容器的指定 Web 服务。
设计与 port_router.py 类似，以 host 网络模式运行。

URL 格式：
  /c/<container-name>/<port-name>/
  示例： /c/alice-x7k2/code-server/
           /c/alice-x7k2/jupyterlab/

路由表键：(container_name, port_name) → (node_ip, node_port)
  同一容器可有多个不同的 Web 端口，各自独立路由。
支持的端口名称（container_ports.name）：
  - code-server
  - jupyterlab
  - web

转发目标：
  node_ip:node_port — 计算节点上 Incus proxy device 监听的端口
                        （NOT 管理节点的对外 host_port）
  Incus proxy 再将请求转发到容器内部 127.0.0.1:container_port。

WebSocket 连接（code-server 终端、Jupyter kernel）通过 TCP 透明代理支持。
"""

import asyncio
import json
import os
import re
import signal
import socket
import time
import urllib.error
import urllib.request

BACKEND_URL = os.environ.get("ROUTER_BACKEND_URL", "http://127.0.0.1:80").rstrip("/")
ROUTER_TOKEN = os.environ.get("PORT_ROUTER_TOKEN", "")
SYNC_INTERVAL = float(os.environ.get("PATH_ROUTER_SYNC_INTERVAL", "5"))
LISTEN_PORT = int(os.environ.get("PATH_PROXY_PORT", "8890"))
CONNECT_TIMEOUT = float(os.environ.get("PATH_PROXY_CONNECT_TIMEOUT", "10"))
PATH_PREFIX = os.environ.get("PATH_PREFIX", "/c/")

# (container_name, port_name) -> (node_ip, node_port)  当前路由表
_routes: dict[tuple[str, str], tuple[str, int]] = {}

_CONTAINER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}$")
_PORT_NAME_RE       = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
# 这些端口名称的服务监听在 /，不理解 base-path ，代理时需裁掉 URL 前缀
# - web: 通用 HTTP 服务（python http.server、Flask 等）
# - code-server: 4.100+ 移除了 --base-path 支持，由代理层处理前缀
# 不裁前缀: jupyterlab（自己配置了 --base-url）
_STRIP_PREFIX_PORTS = frozenset({"web", "code-server"})

# ─── 路由表同步 ───────────────────────────────────────────────────────────────

def _fetch_routes_sync() -> dict[tuple[str, str], tuple[str, int]]:
    request = urllib.request.Request(f"{BACKEND_URL}/api/internal/path-routes")
    if ROUTER_TOKEN:
        request.add_header("X-Port-Router-Token", ROUTER_TOKEN)
    with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    routes: dict[tuple[str, str], tuple[str, int]] = {}
    for item in data.get("routes", []):
        name = str(item.get("container_name", "")).strip()
        port_name = str(item.get("port_name", "")).strip()
        ip = str(item.get("node_ip", "")).strip()
        port = int(item.get("node_port", 0))
        if name and port_name and ip and port > 0:
            routes[(name, port_name)] = (ip, port)
    return routes


async def _sync_loop(stop: asyncio.Event) -> None:
    global _routes
    while not stop.is_set():
        try:
            new_routes = await asyncio.to_thread(_fetch_routes_sync)
            if new_routes != _routes:
                added = set(new_routes) - set(_routes)
                removed = set(_routes) - set(new_routes)
                _routes = new_routes
                if added:
                    print(
                        f"{_ts()} path-proxy routes +{sorted(added)}",
                        flush=True,
                    )
                if removed:
                    print(
                        f"{_ts()} path-proxy routes -{sorted(removed)}",
                        flush=True,
                    )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"{_ts()} path-proxy route sync failed: {exc}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SYNC_INTERVAL)
        except asyncio.TimeoutError:
            pass


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _rewrite_path(path: bytes, container: str, port_name: str) -> bytes:
    """裁掉 /c/<container>/<port>/ 前缀，让通用 Web 服务器看到的是 / 起始的路径。

    /c/alice/web/index.html?q=1  →  /index.html?q=1
    /c/alice/web/                →  /
    /c/alice/web                 →  /
    """
    prefix = (PATH_PREFIX + container + "/" + port_name).encode()  # e.g. b"/c/alice/web"
    if not path.startswith(prefix):
        return path
    rest = path[len(prefix):]   # b"" | b"/" | b"/subdir" | b"/file?q=1"
    return rest if rest else b"/"


def _extract_route_key(path_bytes: bytes) -> tuple[str, str]:
    """从请求路径 /c/<container>/<port-name>/... 提取 (container_name, port_name)。

    转发路径格式：
      /c/alice-x7k2/code-server/index.html  → ('alice-x7k2', 'code-server')
      /c/alice-x7k2/jupyterlab/api/kernels  → ('alice-x7k2', 'jupyterlab')
    """
    prefix = PATH_PREFIX.encode()          # 通常是 b"/c/"
    if not path_bytes.startswith(prefix):
        return "", ""
    rest = path_bytes[len(prefix):]        # b"alice-x7k2/code-server/..."

    # 提取容器名
    slash1 = rest.find(b"/")
    if slash1 < 0:
        return "", ""
    container_bytes = rest[:slash1]

    # 提取端口名
    rest2 = rest[slash1 + 1:]              # b"code-server/..."
    slash2 = rest2.find(b"/")
    port_bytes = rest2[:slash2] if slash2 >= 0 else rest2

    try:
        container = container_bytes.decode("ascii")
        port_name = port_bytes.decode("ascii")
    except Exception:
        return "", ""

    if not _CONTAINER_NAME_RE.match(container):
        return "", ""
    if not port_name or not _PORT_NAME_RE.match(port_name):
        return "", ""

    return container, port_name


async def _read_http_headers(reader: asyncio.StreamReader) -> bytes | None:
    """读取 HTTP 请求头直到 \\r\\n\\r\\n，返回包含分隔符的完整字节串。"""
    buf = b""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=15)
            if not chunk:
                return None
            buf += chunk
            if b"\r\n\r\n" in buf:
                return buf
            if len(buf) > 65536:
                return None  # 请求头过大，拒绝
    except asyncio.TimeoutError:
        return None


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """单向数据转发，读取结束或出错后关闭写端。"""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ─── 连接处理 ─────────────────────────────────────────────────────────────────

async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """处理一条来自 nginx 的 TCP 连接（可能含多个 HTTP 请求或 WebSocket）。"""
    try:
        header_buf = await _read_http_headers(client_reader)
        if not header_buf:
            client_writer.close()
            return

        # 定位头部结束位置
        sep_idx = header_buf.index(b"\r\n\r\n")
        headers_raw = header_buf[:sep_idx]
        after_headers = header_buf[sep_idx + 4:]  # 头部之后的数据（body/WS 帧）

        lines = headers_raw.split(b"\r\n")
        if not lines:
            client_writer.close()
            return

        # 解析请求行，例如：GET /c/my-container/code-server/index.html HTTP/1.1
        req_parts = lines[0].split(b" ")
        if len(req_parts) < 2:
            client_writer.close()
            return

        path = req_parts[1]
        # 如果路径包含查询字符串，只取路径部分
        qs_sep = path.find(b"?")
        path_only = path[:qs_sep] if qs_sep >= 0 else path

        container_name, port_name = _extract_route_key(path_only)
        if not container_name or not port_name:
            _send_error(client_writer, 404, b"Not Found")
            await client_writer.drain()
            client_writer.close()
            return

        route = _routes.get((container_name, port_name))
        if not route:
            _send_error(
                client_writer,
                502,
                f"No web route for {container_name}/{port_name}".encode(),
            )
            await client_writer.drain()
            client_writer.close()
            return

        node_ip, node_port = route

        # ── 路径重写（仅对通用 web 端口）─────────────────────────────────────
        # code-server / jupyterlab 已配置 --base-path / --base-url，无需重写。
        # 命名为 "web" 的端口（普通 HTTP 服务器）监听在 /，需裁掉代理前缀。
        if port_name in _STRIP_PREFIX_PORTS:
            new_path = _rewrite_path(path, container_name, port_name)
            lines[0] = req_parts[0] + b" " + new_path + b" " + req_parts[2]
            header_buf = b"\r\n".join(lines) + b"\r\n\r\n" + after_headers

        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(node_ip, node_port),
                timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:
            _send_error(
                client_writer,
                503,
                f"Cannot connect to container backend: {exc}".encode(),
            )
            await client_writer.drain()
            client_writer.close()
            return

        # 将缓冲的头部数据（及头部后已读取的 body/WS 帧）转发给目标
        target_writer.write(header_buf)
        await target_writer.drain()

        # 双向 TCP 透明代理（支持 HTTP keep-alive 和 WebSocket）
        await asyncio.gather(
            _pipe(client_reader, target_writer),
            _pipe(target_reader, client_writer),
        )
    except Exception:
        pass
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


def _send_error(writer: asyncio.StreamWriter, code: int, body: bytes) -> None:
    phrases = {404: "Not Found", 502: "Bad Gateway", 503: "Service Unavailable"}
    phrase = phrases.get(code, "Error").encode()
    writer.write(
        b"HTTP/1.1 "
        + str(code).encode()
        + b" "
        + phrase
        + b"\r\nContent-Type: text/plain\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


# ─── 主循环 ───────────────────────────────────────────────────────────────────

async def main() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server = await asyncio.start_server(
        _handle,
        "0.0.0.0",
        LISTEN_PORT,
        reuse_address=True,
    )
    print(
        f"{_ts()} http-path-proxy listening on :{LISTEN_PORT} "
        f"(prefix={PATH_PREFIX}, backend={BACKEND_URL})",
        flush=True,
    )

    # 后台路由同步任务
    sync_task = asyncio.create_task(_sync_loop(stop))

    async with server:
        await stop.wait()

    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass

    print(f"{_ts()} http-path-proxy stopped", flush=True)


if __name__ == "__main__":
    if hasattr(socket, "SO_REUSEADDR"):
        asyncio.run(main())
