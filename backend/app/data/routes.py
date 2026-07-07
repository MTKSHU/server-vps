import asyncio
import contextlib
import io
import os
import stat as _stat
import tarfile
import posixpath
import re
import shlex
import shutil
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote


def parse_zfs_size(value: str) -> int:
    """解析 ZFS 返回的大小值，支持纯数字（字节）或带单位（K/M/G/T）"""
    value = value.strip()
    if not value:
        return 0
    # 纯数字
    if re.fullmatch(r"\d+", value):
        return int(value)
    # 带单位：1.5G, 1024M, 1024K, 1.5T
    m = re.fullmatch(r"([\d.]+)\s*([KMGT])", value, re.IGNORECASE)
    if m:
        num = float(m.group(1))
        unit = m.group(2).upper()
        multiplier = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        return int(num * multiplier[unit])
    # 尝试直接转换
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0

import asyncssh
from fastapi import File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from psycopg.types.json import Jsonb

import httpx

from ..config import (
    HF_HTTPS_PROXY,
    HF_STAGING_DIR,
    MAX_UPLOAD_MB,
    NODE_AGENT_FILES_PORT,
    NODE_AGENT_TOKEN,
    SYNC_SSH_IDENTITY_FILE,
    SYNC_SSH_PORT,
    SYNC_SSH_USER,
)
from ..schemas import SharedResourceInfoInput, SharedResourceInput, SharedResourceRequestInput, SharedResourceTagsInput, StorageSettingsInput, UserDirectoryScanInput
from ..auth import is_admin_user, require_admin
from ..platform_settings import get_platform_settings


_deps: dict[str, Any] = {}
_SHARED_RESOURCE_DOWNLOAD_LIMIT = 3
_shared_resource_download_semaphore = asyncio.Semaphore(_SHARED_RESOURCE_DOWNLOAD_LIMIT)
_TRANSFER_CHUNK_SIZE = 1024 * 1024
_transfer_limit_lock = asyncio.Lock()
_transfer_limit_next_at = 0.0


def configure_data_services(deps: dict[str, Any]):
    _deps.update(deps)


def dep(name: str):
    return _deps[name]


def _transfer_bandwidth_limit_mbps() -> int:
    try:
        with dep("db")() as conn:
            return max(0, int(get_platform_settings(conn).get("transfer_bandwidth_limit_mbps") or 0))
    except Exception:
        return 0


async def _throttle_transfer_chunk(chunk_bytes: int, limit_mbps: int) -> None:
    global _transfer_limit_next_at
    if limit_mbps <= 0 or chunk_bytes <= 0:
        return
    bytes_per_second = limit_mbps * 1024 * 1024 / 8
    async with _transfer_limit_lock:
        now = time.monotonic()
        available_at = max(now, _transfer_limit_next_at)
        _transfer_limit_next_at = available_at + chunk_bytes / bytes_per_second
        delay = available_at - now
    if delay > 0:
        await asyncio.sleep(delay)


def _resolve_user_directory_root(conn, user_id: int, policy: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """
    解析用户目录在存储节点上的根路径。
    优先使用 user_storage_datasets.mountpoint（ZFS 实际挂载点），
    回退到 source_path_for_node(policy.home_path, node)（平台路径转换）。
    返回 (node, root_path)。
    """
    # 1) 检查 user_storage_datasets 是否有已应用的 ZFS 挂载点
    dataset = conn.execute(
        """
        SELECT n.*, usd.mountpoint, svr.path AS storage_root
        FROM user_storage_datasets usd
        JOIN nodes n ON n.id = usd.node_id
        LEFT JOIN storage_volume_reports svr ON svr.node_id = n.id AND svr.volume_name = 'root'
        WHERE usd.user_id = %s AND usd.status = 'applied' AND n.status = 'online'
        """,
        (user_id,),
    ).fetchone()
    if dataset and dataset["mountpoint"]:
        mountpoint = str(dataset["mountpoint"]).strip()
        if mountpoint.startswith("/"):
            return dict(dataset), mountpoint.rstrip("/") or "/"
    # 1b) 检查 user_storage_datasets 是否有记录但状态不是 applied（如 pending/creating）
    # 此时 mountpoint 可能已被 agent 更新，但状态还未同步为 applied
    dataset_any = conn.execute(
        """
        SELECT n.*, usd.mountpoint, usd.status, svr.path AS storage_root
        FROM user_storage_datasets usd
        JOIN nodes n ON n.id = usd.node_id
        LEFT JOIN storage_volume_reports svr ON svr.node_id = n.id AND svr.volume_name = 'root'
        WHERE usd.user_id = %s AND n.status = 'online'
        """,
        (user_id,),
    ).fetchone()
    if dataset_any and dataset_any["mountpoint"]:
        mountpoint = str(dataset_any["mountpoint"]).strip()
        if mountpoint.startswith("/"):
            return dict(dataset_any), mountpoint.rstrip("/") or "/"
    # 2) 无可用的 ZFS 挂载点，无法解析用户目录
    raise HTTPException(status_code=400, detail="用户存储数据集尚未就绪，请联系管理员分配存储节点")


def normalize_relative_directory(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        return ""
    parts = value.split("/")
    if any(part in ("", ".", "..") or "\x00" in part for part in parts):
        raise HTTPException(status_code=400, detail="目录相对路径不合法")
    if len(value) > 240:
        raise HTTPException(status_code=400, detail="目录相对路径过长")
    return "/".join(parts)


def normalize_upload_relative_path(value: str) -> str:
    value = value.replace("\\", "/").strip().strip("/")
    if not value:
        raise HTTPException(status_code=400, detail="上传文件名不能为空")
    if len(value) > 512:
        raise HTTPException(status_code=400, detail="上传路径过长")
    parts = value.split("/")
    if any(part in ("", ".", "..") or "\x00" in part for part in parts):
        raise HTTPException(status_code=400, detail="上传路径不合法")
    if any(len(part) > 180 for part in parts):
        raise HTTPException(status_code=400, detail="上传路径片段过长")
    return "/".join(parts)


def source_path_for_node(platform_path: str, node: dict[str, Any]) -> str:
    path = posixpath.normpath(platform_path.strip())
    if not path.startswith("/") or "\x00" in path:
        raise HTTPException(status_code=400, detail="存储路径不合法")
    storage_root = posixpath.normpath(str(node.get("storage_root") or "/data").strip() or "/data")
    if not storage_root.startswith("/") or "\x00" in storage_root:
        raise HTTPException(status_code=400, detail="存储根目录不合法")
    if path == storage_root or path.startswith(storage_root.rstrip("/") + "/"):
        return path
    if path == "/data":
        return storage_root
    if path.startswith("/data/"):
        return posixpath.join(storage_root, path[len("/data/"):])
    return path


def _content_disposition(filename: str) -> str:
    safe_name = filename.replace('"', "")
    return f"attachment; filename*=UTF-8''{quote(safe_name)}"


def _download_filename(relative_path: str, is_directory: bool) -> str:
    base_name = os.path.basename(relative_path.rstrip("/")) if relative_path else ""
    if not base_name:
        base_name = "home"
    if is_directory:
        return f"{base_name}.tar.gz"
    return base_name


async def _sftp_collect_tree(sftp, sftp_path: str, arc_path: str) -> list[tuple]:
    """Recursively collect (sftp_path, arc_path, is_dir, size, mtime) via SFTP."""
    result: list[tuple] = [(sftp_path, arc_path, True, 0, 0)]
    try:
        entries = await sftp.readdir(sftp_path)
    except Exception:
        return result
    for entry in sorted(entries, key=lambda e: e.filename):
        name = entry.filename
        if name in (".", ".."):
            continue
        child_sftp = f"{sftp_path}/{name}"
        child_arc = f"{arc_path}/{name}"
        perm = entry.attrs.permissions or 0
        is_dir = bool(_stat.S_ISDIR(perm))
        if is_dir:
            result.extend(await _sftp_collect_tree(sftp, child_sftp, child_arc))
        else:
            size = entry.attrs.size or 0
            mtime = int(entry.attrs.mtime or 0)
            result.append((child_sftp, child_arc, False, size, mtime))
    return result


async def _sftp_directory_snapshot(
    sftp,
    path: str,
    user_id: int,
    relative_path: str,
    limit: int = 500,
    total_size_bytes: int = 0,
) -> dict[str, Any]:
    entries = []
    total_entries = 0
    try:
        rows = await sftp.readdir(path)
    except Exception as exc:
        return {
            "user_id": user_id,
            "relative_path": relative_path,
            "status": "failed",
            "file_count": 0,
            "size_bytes": total_size_bytes,
            "entries": [],
            "truncated": False,
            "error": str(exc),
            "scanned_at": int(time.time()),
        }
    for entry in sorted(rows, key=lambda item: (item.filename not in (".", ".."), item.filename.lower())):
        name = entry.filename
        if name in (".", ".."):
            continue
        total_entries += 1
        if len(entries) >= limit:
            continue
        perm = entry.attrs.permissions or 0
        if _stat.S_ISDIR(perm):
            entry_type = "directory"
        elif _stat.S_ISLNK(perm):
            entry_type = "symlink"
        else:
            entry_type = "file"
        entries.append({
            "name": name,
            "type": entry_type,
            "size_bytes": int(entry.attrs.size or 0),
            "mtime": int(entry.attrs.mtime or 0),
            "mode": oct(perm)[-4:] if perm else "",
        })
    return {
        "user_id": user_id,
        "relative_path": relative_path,
        "status": "ready",
        "file_count": total_entries,
        "size_bytes": total_size_bytes,
        "entries": entries,
        "truncated": total_entries > limit,
        "error": "",
        "scanned_at": int(time.time()),
    }


def normalize_shared_resource(payload: SharedResourceInput) -> SharedResourceInput:
    payload.name = payload.name.strip()
    payload.version = payload.version.strip() or "default"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", payload.name):
        raise HTTPException(status_code=400, detail="共享资源名称不合法")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,80}", payload.version):
        raise HTTPException(status_code=400, detail="资源提供者不合法")
    payload.source_path = dep("validate_platform_path")(payload.source_path, "源路径")
    payload.mount_path = dep("validate_platform_path")(payload.mount_path, "容器挂载路径")
    payload.tags = normalize_shared_resource_tags(payload.tags)
    return payload


def normalize_shared_resource_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        item = str(tag).strip()
        if not item:
            continue
        if len(item) > 64:
            raise HTTPException(status_code=400, detail=f"标签过长：{item[:16]}")
        if "\x00" in item:
            raise HTTPException(status_code=400, detail="标签不合法")
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    if len(normalized) > 20:
        raise HTTPException(status_code=400, detail="标签最多 20 个")
    return normalized


def upsert_tag_options(conn, tags: list[str]) -> None:
    """Save new tags to the shared tag library (idempotent)."""
    if not tags:
        return
    ts = int(time.time())
    for tag in tags:
        conn.execute(
            "INSERT INTO resource_tag_options (tag, created_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tag, ts),
        )


def ensure_user_data_policy(conn, user: dict[str, Any]) -> dict[str, Any]:
    policy = conn.execute("SELECT * FROM user_data_policies WHERE user_id = %s", (user["id"],)).fetchone()
    if policy:
        return policy
    ts = dep("now_ts")()
    policy = conn.execute(
        """
        INSERT INTO user_data_policies (
            user_id, home_path, backup_enabled, sync_on_create, sync_on_stop,
            backup_interval_hours, created_at, updated_at
        ) VALUES (%s, %s, TRUE, TRUE, FALSE, 24, %s, %s)
        RETURNING *
        """,
        (user["id"], f"{get_storage_settings(conn).get('user_base_path', '/data/users').rstrip('/')}/{user['username']}", ts, ts),
    ).fetchone()
    ensure_user_zfs_dataset_task = _deps.get("ensure_user_zfs_dataset_task")
    if ensure_user_zfs_dataset_task and user.get("enabled", True):
        ensure_user_zfs_dataset_task(conn, user["id"], "policy-create")
    return policy


def public_shared_resource(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "resource_type": row["resource_type"],
        "name": row["name"],
        "version": row["version"],
        "source_path": row["source_path"],
        "mount_path": row["mount_path"],
        "tags": list(row.get("tags") or []),
        "readonly": row["readonly"],
        "sync_policy": row["sync_policy"],
        "enabled": row["enabled"],
        "size_bytes": row["size_bytes"],
        "file_count": row["file_count"],
        "check_status": row["check_status"],
        "check_error": row["check_error"],
        "checked_at": row["checked_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "source_url": row.get("source_url", ""),
        "request_status": row.get("request_status", "ready"),
        "requested_by": row.get("requested_by"),
        "download_progress": row.get("download_progress") or {},
    }


# ─── 后端主动下载 HuggingFace 并推送到存储节点 ──────────────────────────────
# 排除 huggingface-cli 写入的元数据缓存目录，不传到存储节点
_HF_EXCLUDE = {".cache"}
_TEXT_PREVIEW_EXTS = {".txt", ".py", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".md", ".sh", ".log", ".csv", ".tsv", ".xml", ".env"}
_IMAGE_PREVIEW_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_VIDEO_PREVIEW_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
_PDF_PREVIEW_MIME = {".pdf": "application/pdf"}
_TEXT_PREVIEW_LIMIT = 256 * 1024
_IMAGE_PREVIEW_LIMIT = 3 * 1024 * 1024
_VIDEO_PREVIEW_LIMIT = 100 * 1024 * 1024
_PDF_PREVIEW_LIMIT = 100 * 1024 * 1024


def _iter_visible_files(paths: list[str]):
    for path in paths:
        name = os.path.basename(path.rstrip("/"))
        if name.startswith(".") or name in _HF_EXCLUDE:
            continue
        if os.path.isfile(path):
            yield path
            continue
        if not os.path.isdir(path):
            continue
        for root, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _HF_EXCLUDE]
            for filename in filenames:
                if filename.startswith(".") or filename in _HF_EXCLUDE:
                    continue
                full = os.path.join(root, filename)
                if os.path.isfile(full):
                    yield full


def _scan_visible_files(paths: list[str]) -> tuple[int, int, str]:
    count = 0
    size = 0
    latest_file = ""
    latest_mtime = -1.0
    for full in _iter_visible_files(paths):
        count += 1
        try:
            stat = os.stat(full)
        except OSError:
            continue
        size += max(0, int(stat.st_size))
        if stat.st_mtime >= latest_mtime:
            latest_mtime = stat.st_mtime
            latest_file = os.path.basename(full)[:60]
    return count, size, latest_file


def _shared_resource_ssh_kwargs(node: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(node, dict):
        host = str(node.get("ip") or "").strip()
        node_user = str(node.get("ssh_user") or "").strip()
        node_port_raw = node.get("ssh_port")
    else:
        host = str(node).strip()
        node_user = ""
        node_port_raw = None

    if not host:
        raise HTTPException(status_code=400, detail="存储节点地址缺失")

    ssh_user = node_user or SYNC_SSH_USER
    try:
        ssh_port = int(node_port_raw) if node_port_raw else SYNC_SSH_PORT
    except (TypeError, ValueError):
        ssh_port = SYNC_SSH_PORT

    connect_kwargs: dict[str, Any] = dict(
        host=host,
        port=ssh_port,
        username=ssh_user,
        known_hosts=None,
    )
    # 确定密钥：优先 SYNC_SSH_IDENTITY_FILE，次选后端自管理的 .cluster_node_key
    _identity: str = ""
    if SYNC_SSH_IDENTITY_FILE and os.path.isfile(SYNC_SSH_IDENTITY_FILE):
        _identity = SYNC_SSH_IDENTITY_FILE
    else:
        _cluster_key = os.path.join(
            os.environ.get("AGENT_RELEASE_DIR", "/var/lib/cluster-agent-releases"),
            ".cluster_node_key",
        )
        if os.path.isfile(_cluster_key):
            _identity = _cluster_key
    if _identity:
        connect_kwargs["client_keys"] = [_identity]
    return connect_kwargs


# ---------------------------------------------------------------------------
# 存储节点 SSH 连接池
#
# 目录浏览等只读、短命令原本每次都新建 SSH 连接（TCP 握手 + 认证约
# 100-500ms），在同步任务占满存储节点 IO 时尤其卡顿。这里按
# (host, port, user) 维护一个常驻连接，asyncssh 支持在单连接上多路复用
# 多个会话，因此并发的 ls/zfs 命令可共享同一连接。连接失效时自动重连并
# 重试一次。仅用于快速只读命令；大文件 SFTP 传输仍使用各自独立连接。
# ---------------------------------------------------------------------------
_ssh_pool: dict[tuple[str, int, str], Any] = {}
_ssh_pool_locks: dict[tuple[str, int, str], asyncio.Lock] = {}
_ssh_pool_guard = asyncio.Lock()


_SSH_CONNECT_TIMEOUT = 15   # 秒：建立连接的超时
_SSH_COMMAND_TIMEOUT = 60   # 秒：单条 SSH 命令（ls/zfs get）的超时


async def _with_pooled_ssh(node: dict[str, Any] | str, func: Callable[[Any], Awaitable[Any]]) -> Any:
    """在池化的 SSH 连接上执行 func(ssh)，连接失效时重连并重试一次。

    * connect_timeout：防止存储节点不可达时无限挂起。
    * asyncio.wait_for：防止 ls/zfs get 等命令因节点 IO 卡死而永久阻塞。
    """
    connect_kwargs = _shared_resource_ssh_kwargs(node)
    key = (connect_kwargs["host"], connect_kwargs["port"], connect_kwargs["username"])
    async with _ssh_pool_guard:
        lock = _ssh_pool_locks.setdefault(key, asyncio.Lock())
    last_exc: Exception | None = None
    for attempt in range(2):
        async with lock:
            conn = _ssh_pool.get(key)
            if conn is None:
                try:
                    conn = await asyncssh.connect(
                        **connect_kwargs,
                        keepalive_interval=30,
                        connect_timeout=_SSH_CONNECT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise HTTPException(status_code=504, detail="连接存储节点超时，请稍后重试")
                _ssh_pool[key] = conn
        try:
            return await asyncio.wait_for(func(conn), timeout=_SSH_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            # 命令超时：连接可能已进入异常状态，驱逐出池
            async with lock:
                stale = _ssh_pool.pop(key, None)
            if stale is not None:
                with contextlib.suppress(Exception):
                    stale.close()
            raise HTTPException(status_code=504, detail="存储节点响应超时，请稍后重试")
        except (asyncssh.Error, OSError) as exc:
            last_exc = exc
            async with lock:
                stale = _ssh_pool.pop(key, None)
            if stale is not None:
                with contextlib.suppress(Exception):
                    stale.close()
            # 循环重试一次（重新建立连接）
    if last_exc is not None:
        raise last_exc
    return None


# ZFS 用量缓存：dataset 用量变化较慢，避免每次访问根目录 / 每次自动刷新
# 都额外发一次 `zfs get used` SSH 命令。
_zfs_usage_cache: dict[tuple[int, str], tuple[float, int]] = {}
_ZFS_USAGE_TTL = 30.0


def _cached_zfs_usage(node_id: int, dataset_name: str) -> int | None:
    entry = _zfs_usage_cache.get((node_id, dataset_name))
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None


def _store_zfs_usage(node_id: int, dataset_name: str, size_bytes: int) -> None:
    _zfs_usage_cache[(node_id, dataset_name)] = (time.monotonic() + _ZFS_USAGE_TTL, size_bytes)


# ---------------------------------------------------------------------------
# Agent HTTP 文件列表 API
#
# node-agent 内置了 GET /api/files/ls 端点，可直接读取本地文件系统，
# 无需 SSH，响应时间 < 10ms（LAN 内）。仅当 NODE_AGENT_TOKEN 已配置且
# 节点运行了新版 agent（支持 --files-port）时生效；否则自动回退到 SSH。
# ---------------------------------------------------------------------------
_agent_http_client: httpx.AsyncClient | None = None
_agent_http_lock = asyncio.Lock()


async def _get_agent_http_client() -> httpx.AsyncClient:
    global _agent_http_client
    if _agent_http_client is None or _agent_http_client.is_closed:
        async with _agent_http_lock:
            if _agent_http_client is None or _agent_http_client.is_closed:
                _agent_http_client = httpx.AsyncClient(timeout=5.0)
    return _agent_http_client


async def _list_via_agent_http(
    node: dict[str, Any],
    absolute_path: str,
    root_path: str,
    limit: int = 500,
    zfs_dataset: str = "",
) -> dict[str, Any] | None:
    """通过 agent HTTP API 列出目录，比 SSH ls 快约 10x。
    失败（agent 不可用或版本过旧）时返回 None，由调用方回退到 SSH。
    """
    if not NODE_AGENT_TOKEN:
        return None
    node_ip = str(node.get("ip") or "").strip()
    if not node_ip:
        return None
    params: dict[str, Any] = {"path": absolute_path, "root": root_path, "limit": limit}
    if zfs_dataset:
        params["zfs_dataset"] = zfs_dataset
    url = f"http://{node_ip}:{NODE_AGENT_FILES_PORT}/api/files/ls"
    try:
        client = await _get_agent_http_client()
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {NODE_AGENT_TOKEN}"})
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# SSH 连接池预热 / 保活
#
# 后端启动时对所有活跃存储节点主动建立 SSH 连接，消除首次访问的冷启动延迟。
# 每 50 秒运行一次 keep-alive（SSH 默认 60s 超时），防止连接被中间设备断开。
# ---------------------------------------------------------------------------
async def _ssh_warmup_loop() -> None:
    import sys
    # 启动后稍等几秒，待 DB 连接就绪，然后立即预热一次，之后每 50 秒保活
    await asyncio.sleep(5)
    while True:
        try:
            with dep("db")() as conn:
                nodes = conn.execute(
                    """
                    SELECT DISTINCT n.* FROM nodes n
                    JOIN user_storage_datasets usd ON usd.node_id = n.id
                    WHERE n.status = 'online'
                    """
                ).fetchall()
            for node in nodes:
                try:
                    await asyncio.wait_for(
                        _with_pooled_ssh(dict(node), lambda ssh: ssh.run("true", check=False)),
                        timeout=10,
                    )
                except Exception:
                    pass
        except Exception as exc:
            print(f"[WARN] SSH warmup error: {exc!r}", file=sys.stderr, flush=True)
        await asyncio.sleep(50)


def start_data_background_tasks() -> None:
    """在 FastAPI startup 事件中调用，启动数据模块后台任务。"""
    asyncio.create_task(_ssh_warmup_loop())


async def _upload_entries_incremental(
    ssh,
    sftp,
    *,
    temp_dir: str,
    entries: list[str],
    target_path: str,
    sftp_state: dict[str, Any],
    sftp_seen: set[str],
    sftp_transferred: dict[str, int],
    progress_handler,
    bandwidth_limit_mbps: int = 0,
) -> None:
    target_root = target_path.rstrip("/") or "/"
    ensured_dirs: set[str] = set()
    for src in sorted(_iter_visible_files(entries)):
        rel_path = os.path.relpath(src, temp_dir).replace(os.sep, "/")
        remote_path = posixpath.join(target_root, rel_path)
        remote_parent = posixpath.dirname(remote_path) or "/"
        if remote_parent not in ensured_dirs:
            await ssh.run(f"mkdir -p {shlex.quote(remote_parent)}", check=True)
            ensured_dirs.add(remote_parent)

        local_size = os.path.getsize(src)
        remote_size = -1
        try:
            remote_stat = await sftp.stat(remote_path)
            remote_size = int(getattr(remote_stat, "size", -1) or -1)
        except Exception:
            remote_size = -1

        if remote_size == local_size:
            sftp_state["current_file"] = os.path.basename(src)[:60]
            if src not in sftp_seen:
                sftp_seen.add(src)
                sftp_state["files_done"] += 1
            sftp_state["bytes_done"] += max(0, local_size - sftp_transferred.get(src, 0))
            sftp_transferred[src] = local_size
            continue

        transferred = 0
        sftp_state["current_file"] = os.path.basename(src)[:60]
        async with sftp.open(remote_path, "wb") as remote_file:
            with open(src, "rb") as local_file:
                while True:
                    chunk = await asyncio.to_thread(local_file.read, _TRANSFER_CHUNK_SIZE)
                    if not chunk:
                        break
                    await _throttle_transfer_chunk(len(chunk), bandwidth_limit_mbps)
                    await remote_file.write(chunk)
                    transferred += len(chunk)
                    if progress_handler:
                        progress_handler(src, remote_path, transferred, local_size)
        if local_size == 0 and progress_handler:
            progress_handler(src, remote_path, 0, 0)


async def _preview_shared_resource_file(node: dict[str, Any] | str, absolute_path: str) -> dict[str, Any]:
    ext = os.path.splitext(absolute_path)[1].lower()
    async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
        async with ssh.start_sftp_client() as sftp:
            stat = await sftp.stat(absolute_path)
            if getattr(stat, "size", 0) < 0:
                raise HTTPException(status_code=400, detail="文件大小不可用")
            size_bytes = int(stat.size)
            if ext in _TEXT_PREVIEW_EXTS:
                if size_bytes > _TEXT_PREVIEW_LIMIT:
                    return {"kind": "too_large", "size_bytes": size_bytes, "message": f"文本文件超过 {bytes_to_human(_TEXT_PREVIEW_LIMIT)}，不在页面内预览"}
                async with sftp.open(absolute_path, "rb") as remote_file:
                    raw = await remote_file.read()
                return {
                    "kind": "text",
                    "size_bytes": size_bytes,
                    "text": raw.decode("utf-8", errors="replace"),
                    "mime": "text/plain; charset=utf-8",
                }
            if ext in _IMAGE_PREVIEW_MIME:
                if size_bytes > _IMAGE_PREVIEW_LIMIT:
                    return {"kind": "too_large", "size_bytes": size_bytes, "message": f"图片超过 {bytes_to_human(_IMAGE_PREVIEW_LIMIT)}，不在页面内预览"}
                async with sftp.open(absolute_path, "rb") as remote_file:
                    raw = await remote_file.read()
                return {
                    "kind": "image",
                    "size_bytes": size_bytes,
                    "mime": _IMAGE_PREVIEW_MIME[ext],
                    "data": raw.hex(),
                    "encoding": "hex",
                }
            if ext in _VIDEO_PREVIEW_MIME:
                if size_bytes > _VIDEO_PREVIEW_LIMIT:
                    return {"kind": "too_large", "size_bytes": size_bytes, "message": f"视频超过 {bytes_to_human(_VIDEO_PREVIEW_LIMIT)}，不在页面内预览"}
                async with sftp.open(absolute_path, "rb") as remote_file:
                    raw = await remote_file.read()
                return {
                    "kind": "video",
                    "size_bytes": size_bytes,
                    "mime": _VIDEO_PREVIEW_MIME[ext],
                    "data": raw.hex(),
                    "encoding": "hex",
                }
            if ext in _PDF_PREVIEW_MIME:
                if size_bytes > _PDF_PREVIEW_LIMIT:
                    return {"kind": "too_large", "size_bytes": size_bytes, "message": f"PDF 超过 {bytes_to_human(_PDF_PREVIEW_LIMIT)}，不在页面内预览"}
                async with sftp.open(absolute_path, "rb") as remote_file:
                    raw = await remote_file.read()
                return {
                    "kind": "pdf",
                    "size_bytes": size_bytes,
                    "mime": _PDF_PREVIEW_MIME[ext],
                    "data": raw.hex(),
                    "encoding": "hex",
                }
    return {"kind": "unsupported", "size_bytes": size_bytes, "message": "当前仅支持预览小文本、小图片、小视频和 PDF 文件"}


def bytes_to_human(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def enqueue_shared_resource_verify_task(
    conn,
    resource: dict[str, Any],
    node: dict[str, Any],
    source_path: str,
    delay_seconds: int = 0,
) -> dict[str, Any]:
    ts = dep("now_ts")()
    conn.execute(
        """
        UPDATE shared_resources
        SET check_status = 'checking', check_error = '', updated_at = %s
        WHERE id = %s
        """,
        (ts, resource["id"]),
    )
    return dep("enqueue_node_task")(
        conn,
        node["id"],
        None,
        "verify_shared_resource",
        {
            "resource_id": resource["id"],
            "resource_type": resource["resource_type"],
            "name": resource["name"],
            "version": resource["version"],
            "source_path": source_path,
        },
        available_at=ts + max(0, delay_seconds),
    )


def schedule_shared_resource_auto_verify(resource_id: int, node: dict[str, Any], source_path: str) -> None:
    try:
        with dep("db")() as conn:
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                return
            task = enqueue_shared_resource_verify_task(conn, resource, node, source_path, delay_seconds=30)
            dep("audit")(
                conn,
                "system",
                "auto-verify",
                f"shared-resource:{resource_id}",
                {"node": node["hostname"], "path": source_path, "task_id": task["id"], "delay_seconds": 30},
            )
    except Exception:
        pass


def mark_shared_resource_download_queued(resource_id: int) -> None:
    try:
        with dep("db")() as conn:
            conn.execute(
                """
                UPDATE shared_resources
                SET download_progress = %s, updated_at = %s
                WHERE id = %s
                """,
                (
                    Jsonb(
                        {
                            "phase": "queued",
                            "pct": 0,
                            "current_file": "",
                            "queue_limit": _SHARED_RESOURCE_DOWNLOAD_LIMIT,
                        }
                    ),
                    dep("now_ts")(),
                    resource_id,
                ),
            )
    except Exception:
        pass


async def run_queued_shared_resource_download(resource_id: int, runner: Callable[[], Awaitable[None]]) -> None:
    mark_shared_resource_download_queued(resource_id)
    async with _shared_resource_download_semaphore:
        await runner()


async def backend_hf_download_and_sync(
    resource_id: int,
    hf_repo_id: str,
    hf_revision: str,
    hf_token: str,
    hf_endpoint: str,
    repo_type: str,
    node: dict[str, Any] | str,
    target_path: str,
) -> None:
    """在后端服务器运行 huggingface-cli download，然后通过 asyncssh SFTP 推送到存储节点。"""
    temp_dir = os.path.join(HF_STAGING_DIR, f"hf-{resource_id}")
    _prog: dict[str, Any] = {}
    keep_staging = False

    def _flush(status: str | None = None, error: str = "") -> None:
        try:
            with dep("db")() as conn:
                if status is not None:
                    conn.execute(
                        "UPDATE shared_resources "
                        "SET request_status=%s, check_error=%s, download_progress=%s, updated_at=%s "
                        "WHERE id=%s",
                        (status, error[:2000], Jsonb(_prog), dep("now_ts")(), resource_id),
                    )
                else:
                    conn.execute(
                        "UPDATE shared_resources SET download_progress=%s, updated_at=%s WHERE id=%s",
                        (Jsonb(_prog), dep("now_ts")(), resource_id),
                    )
        except Exception:
            pass

    try:
        os.makedirs(temp_dir, exist_ok=True)

        # ── Stage 1: HuggingFace download ──────────────────────────────
        _prog.update(phase="downloading", files_done=0, files_total=0, bytes_done=0, pct=0, current_file="")

        cli_args = [
            "hf", "download", hf_repo_id,
            "--local-dir", temp_dir,
            "--revision", hf_revision,
        ]
        if repo_type == "dataset":
            cli_args += ["--repo-type", "dataset"]

        env = {**os.environ}
        if hf_token:
            env["HF_TOKEN"] = hf_token
        if hf_endpoint:
            env["HF_ENDPOINT"] = hf_endpoint
        # 代理：优先用用户请求中的设置（未来扩展），否则用系统配置
        if HF_HTTPS_PROXY and "HTTPS_PROXY" not in env:
            env["HTTPS_PROXY"] = HF_HTTPS_PROXY
            env["https_proxy"] = HF_HTTPS_PROXY

        dl_proc = await asyncio.create_subprocess_exec(
            *cli_args, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # 合并 stderr→stdout 统一读取
        )
        _out_chunks: list[bytes] = []

        async def _read_stderr() -> None:
            buf = b""
            while True:
                chunk = await dl_proc.stdout.read(4096)
                if not chunk:
                    break
                _out_chunks.append(chunk)
                buf += chunk
                parts = re.split(b"[\r\n]", buf)
                buf = parts[-1]
                for part in parts[:-1]:
                    text = part.decode(errors="replace")
                    # 整体进度："Fetching 45 files:  11%|..."
                    m = re.search(r"Fetching (\d+) files:\s+(\d+)%", text)
                    if m:
                        _prog["files_total"] = int(m.group(1))
                        _prog["pct"] = int(m.group(2))
                    # 单文件进度："Downloading filename.bin:  45%|..."
                    m2 = re.search(r"Downloading ([^:]+?):\s+(\d+)%", text)
                    if m2:
                        _prog["current_file"] = m2.group(1).strip()[-60:]

        async def _poll_dl() -> None:
            while True:
                await asyncio.sleep(3)
                try:
                    files_done, bytes_done, latest_file = _scan_visible_files([temp_dir])
                    _prog["files_done"] = files_done
                    _prog["bytes_done"] = bytes_done
                    if latest_file and not _prog.get("current_file"):
                        _prog["current_file"] = latest_file
                    if _prog.get("files_total"):
                        approx_pct = min(99, int(files_done * 100 / max(1, int(_prog["files_total"]))))
                        _prog["pct"] = max(int(_prog.get("pct") or 0), approx_pct)
                except OSError:
                    pass
                _flush()

        _poll_task = asyncio.create_task(_poll_dl())
        await asyncio.gather(_read_stderr(), dl_proc.wait())
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass

        if dl_proc.returncode != 0:
            err = b"".join(_out_chunks).decode(errors="replace")
            raise RuntimeError(f"hf download 下载失败:\n{err[-2000:]}")

        # ── Stage 2: SFTP push to storage node ────────────────────────
        connect_kwargs = _shared_resource_ssh_kwargs(node)

        entries = [
            os.path.join(temp_dir, e)
            for e in os.listdir(temp_dir)
            if e not in _HF_EXCLUDE
        ]

        upload_files_total, upload_bytes_total, _ = _scan_visible_files(entries)
        _prog.update(
            phase="uploading",
            pct=0,
            current_file="",
            files_done=0,
            files_total=upload_files_total,
            bytes_done=0,
            bytes_total=upload_bytes_total,
        )
        _flush()

        _sftp_state: dict[str, Any] = {"current_file": "", "bytes_done": 0, "files_done": 0}
        _sftp_seen: set[str] = set()
        _sftp_transferred: dict[str, int] = {}
        bandwidth_limit_mbps = _transfer_bandwidth_limit_mbps()

        def _sftp_progress(src: str, dst: str, transferred: int, total: int) -> None:
            _sftp_state["current_file"] = os.path.basename(src)[:60]
            prev = _sftp_transferred.get(src, 0)
            if transferred > prev:
                _sftp_state["bytes_done"] += transferred - prev
                _sftp_transferred[src] = transferred
            if transferred >= total and src not in _sftp_seen:
                _sftp_seen.add(src)
                _sftp_state["files_done"] += 1

        async def _poll_sftp() -> None:
            while True:
                await asyncio.sleep(3)
                _prog["current_file"] = _sftp_state.get("current_file", "")
                _prog["files_done"] = int(_sftp_state.get("files_done") or 0)
                _prog["bytes_done"] = int(_sftp_state.get("bytes_done") or 0)
                total = int(_prog.get("bytes_total") or 0)
                if total > 0:
                    _prog["pct"] = min(99, int(_prog["bytes_done"] * 100 / total))
                _flush()

        _sftp_poll = asyncio.create_task(_poll_sftp())
        async with asyncssh.connect(**connect_kwargs) as ssh:
            await ssh.run(f"mkdir -p {shlex.quote(target_path)}", check=True)
            if entries:
                async with ssh.start_sftp_client() as sftp:
                    await _upload_entries_incremental(
                        ssh,
                        sftp,
                        temp_dir=temp_dir,
                        entries=entries,
                        target_path=target_path,
                        sftp_state=_sftp_state,
                        sftp_seen=_sftp_seen,
                        sftp_transferred=_sftp_transferred,
                        progress_handler=_sftp_progress,
                        bandwidth_limit_mbps=bandwidth_limit_mbps,
                    )
        _sftp_poll.cancel()
        try:
            await _sftp_poll
        except asyncio.CancelledError:
            pass

        _prog.update(phase="done", pct=100, current_file="", files_done=upload_files_total, bytes_done=upload_bytes_total)
        _flush(status="ready")
        schedule_shared_resource_auto_verify(resource_id, node, target_path)

    except Exception as exc:
        keep_staging = True
        _prog.update(phase="error")
        _flush(status="failed", error=str(exc))

    finally:
        if not keep_staging:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ─── 后端主动从 ModelScope 下载并推送到存储节点 ─────────────────────────────
async def backend_ms_download_and_sync(
    resource_id: int,
    ms_repo_id: str,
    ms_revision: str,
    ms_token: str,
    repo_type: str,
    node: dict[str, Any] | str,
    target_path: str,
) -> None:
    """使用 ModelScope SDK 下载数据集/模型，然后通过 asyncssh SFTP 推送到存储节点。"""
    temp_dir = os.path.join(HF_STAGING_DIR, f"ms-{resource_id}")
    _prog: dict[str, Any] = {}
    keep_staging = False

    def _flush(status: str | None = None, error: str = "") -> None:
        try:
            with dep("db")() as conn:
                if status is not None:
                    conn.execute(
                        "UPDATE shared_resources "
                        "SET request_status=%s, check_error=%s, download_progress=%s, updated_at=%s "
                        "WHERE id=%s",
                        (status, error[:2000], Jsonb(_prog), dep("now_ts")(), resource_id),
                    )
                else:
                    conn.execute(
                        "UPDATE shared_resources SET download_progress=%s, updated_at=%s WHERE id=%s",
                        (Jsonb(_prog), dep("now_ts")(), resource_id),
                    )
        except Exception:
            pass

    try:
        os.makedirs(temp_dir, exist_ok=True)

        # ── Stage 1: ModelScope download ────────────────────────────────
        _prog.update(phase="downloading", files_done=0, files_total=0, bytes_done=0, pct=0, current_file="")
        _flush()

        ms_repo_type = "dataset" if repo_type == "dataset" else "model"

        def _parse_ms_progress(text: str) -> None:
            m = re.search(r"Got (\d+) files, start to download", text)
            if m:
                _prog["files_total"] = int(m.group(1))
            m = re.search(r"Processing (\d+) items:\s+(\d+)%", text)
            if m:
                _prog["files_total"] = max(int(_prog.get("files_total") or 0), int(m.group(1)))
                _prog["pct"] = int(m.group(2))
            m = re.search(r"Downloading \[([^\]]+)\]:\s+(\d+)%", text)
            if m:
                _prog["current_file"] = m.group(1).strip()[-60:]

        class _ProgressCapture(io.TextIOBase):
            def __init__(self):
                self._buf = ""

            def write(self, s: str) -> int:
                if not s:
                    return 0
                self._buf += s
                parts = re.split(r"[\r\n]", self._buf)
                self._buf = parts[-1]
                for part in parts[:-1]:
                    if part.strip():
                        _parse_ms_progress(part)
                return len(s)

            def flush(self) -> None:
                if self._buf.strip():
                    _parse_ms_progress(self._buf)
                self._buf = ""

        def _do_download() -> None:
            from modelscope.hub.snapshot_download import snapshot_download
            kwargs: dict[str, Any] = dict(
                model_id=ms_repo_id,
                repo_type=ms_repo_type,
                local_dir=temp_dir,
                revision=ms_revision or None,
            )
            if ms_token:
                kwargs["token"] = ms_token
            capture = _ProgressCapture()
            with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                snapshot_download(**kwargs)
            capture.flush()

        async def _poll_ms() -> None:
            while True:
                await asyncio.sleep(3)
                try:
                    files_done, bytes_done, latest_file = _scan_visible_files([temp_dir])
                    _prog["files_done"] = files_done
                    _prog["bytes_done"] = bytes_done
                    if latest_file and not _prog.get("current_file"):
                        _prog["current_file"] = latest_file
                    if _prog.get("files_total"):
                        approx_pct = min(99, int(files_done * 100 / max(1, int(_prog["files_total"]))))
                        _prog["pct"] = max(int(_prog.get("pct") or 0), approx_pct)
                except OSError:
                    pass
                _flush()

        _poll_task = asyncio.create_task(_poll_ms())
        try:
            await asyncio.to_thread(_do_download)
        finally:
            _poll_task.cancel()
            try:
                await _poll_task
            except asyncio.CancelledError:
                pass

        # ── Stage 2: SFTP push to storage node ──────────────────────────
        connect_kwargs = _shared_resource_ssh_kwargs(node)

        entries = [
            os.path.join(temp_dir, e)
            for e in os.listdir(temp_dir)
            if e not in _HF_EXCLUDE
        ]

        upload_files_total, upload_bytes_total, _ = _scan_visible_files(entries)
        _prog.update(
            phase="uploading",
            pct=0,
            current_file="",
            files_done=0,
            files_total=upload_files_total,
            bytes_done=0,
            bytes_total=upload_bytes_total,
        )
        _flush()

        _sftp_state: dict[str, Any] = {"current_file": "", "bytes_done": 0, "files_done": 0}
        _sftp_seen: set[str] = set()
        _sftp_transferred: dict[str, int] = {}
        bandwidth_limit_mbps = _transfer_bandwidth_limit_mbps()

        def _sftp_progress(src: str, dst: str, transferred: int, total: int) -> None:
            _sftp_state["current_file"] = os.path.basename(src)[:60]
            prev = _sftp_transferred.get(src, 0)
            if transferred > prev:
                _sftp_state["bytes_done"] += transferred - prev
                _sftp_transferred[src] = transferred
            if transferred >= total and src not in _sftp_seen:
                _sftp_seen.add(src)
                _sftp_state["files_done"] += 1

        async def _poll_sftp() -> None:
            while True:
                await asyncio.sleep(3)
                _prog["current_file"] = _sftp_state.get("current_file", "")
                _prog["files_done"] = int(_sftp_state.get("files_done") or 0)
                _prog["bytes_done"] = int(_sftp_state.get("bytes_done") or 0)
                total = int(_prog.get("bytes_total") or 0)
                if total > 0:
                    _prog["pct"] = min(99, int(_prog["bytes_done"] * 100 / total))
                _flush()

        _sftp_poll = asyncio.create_task(_poll_sftp())
        async with asyncssh.connect(**connect_kwargs) as ssh:
            await ssh.run(f"mkdir -p {shlex.quote(target_path)}", check=True)
            if entries:
                async with ssh.start_sftp_client() as sftp:
                    await _upload_entries_incremental(
                        ssh,
                        sftp,
                        temp_dir=temp_dir,
                        entries=entries,
                        target_path=target_path,
                        sftp_state=_sftp_state,
                        sftp_seen=_sftp_seen,
                        sftp_transferred=_sftp_transferred,
                        progress_handler=_sftp_progress,
                        bandwidth_limit_mbps=bandwidth_limit_mbps,
                    )
        _sftp_poll.cancel()
        try:
            await _sftp_poll
        except asyncio.CancelledError:
            pass

        _prog.update(phase="done", pct=100, current_file="", files_done=upload_files_total, bytes_done=upload_bytes_total)
        _flush(status="ready")
        schedule_shared_resource_auto_verify(resource_id, node, target_path)

    except Exception as exc:
        keep_staging = True
        _prog.update(phase="error")
        _flush(status="failed", error=str(exc))

    finally:
        if not keep_staging:
            shutil.rmtree(temp_dir, ignore_errors=True)


STORAGE_SETTING_KEYS = {
    "dataset_base_path": "/data/datasets",
    "model_base_path": "/data/models/huggingface",
    "user_base_path": "/data/users",
    "hf_endpoint": "",
    "hf_endpoint_enabled": "0",
}


def get_storage_settings(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM system_settings WHERE key = ANY(%s)",
        (list(STORAGE_SETTING_KEYS.keys()),),
    ).fetchall()
    settings = dict(STORAGE_SETTING_KEYS)
    for row in rows:
        settings[row["key"]] = row["value"]
    return settings


def public_storage_settings(settings: dict) -> dict:
    return {
        "dataset_base_path": settings["dataset_base_path"],
        "model_base_path": settings["model_base_path"],
        "user_base_path": settings["user_base_path"],
        "hf_endpoint": settings["hf_endpoint"],
        "hf_endpoint_enabled": settings["hf_endpoint_enabled"] == "1",
    }


def register_data_routes(app, deps: dict[str, Any]):
    configure_data_services(deps)
    db = deps["db"]
    current_user = deps["current_user"]
    now_ts = deps["now_ts"]
    audit = deps["audit"]
    public_task = deps["public_task"]
    enqueue_node_task = deps["enqueue_node_task"]
    select_storage_node_for_path = deps["select_storage_node_for_path"]

    @app.get("/api/data/resource-tag-options")
    def resource_tag_options_get():
        with db() as conn:
            current_user(conn)
            rows = conn.execute(
                "SELECT tag FROM resource_tag_options ORDER BY tag"
            ).fetchall()
            return [row["tag"] for row in rows]

    @app.get("/api/data/storage-settings")
    def storage_settings_get():
        with db() as conn:
            current_user(conn)
            return public_storage_settings(get_storage_settings(conn))

    @app.put("/api/data/storage-settings")
    def storage_settings_put(payload: StorageSettingsInput):
        require_admin()
        ts = now_ts()
        with db() as conn:
            actor = current_user(conn)
            upsert_sql = """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                    """
            for key in ("dataset_base_path", "model_base_path", "user_base_path"):
                value = getattr(payload, key).strip()
                dep("validate_platform_path")(value, f"存储路径({key})")
                conn.execute(upsert_sql, (key, value, ts))
            hf_endpoint = payload.hf_endpoint.strip()
            if hf_endpoint and not re.fullmatch(r"https?://[A-Za-z0-9._:/-]{1,200}", hf_endpoint):
                raise HTTPException(status_code=400, detail="HF 镜像站地址不合法")
            conn.execute(upsert_sql, ("hf_endpoint", hf_endpoint, ts))
            conn.execute(upsert_sql, ("hf_endpoint_enabled", "1" if payload.hf_endpoint_enabled else "0", ts))
            audit(conn, actor["username"], "update", "system-settings:storage", payload.model_dump())
            return public_storage_settings(get_storage_settings(conn))

    @app.get("/api/data/user-policies")
    def user_data_policies():
        """返回所有用户数据策略列表（含 ZFS 挂载点信息），管理员看全部，普通用户只看自己"""
        with db() as conn:
            actor = current_user(conn)
            if is_admin_user(actor):
                rows = conn.execute(
                    """
                    SELECT udp.*, u.username, usd.mountpoint AS zfs_mountpoint, usd.status AS zfs_status
                    FROM user_data_policies udp
                    JOIN users u ON u.id = udp.user_id
                    LEFT JOIN user_storage_datasets usd ON usd.user_id = udp.user_id
                    ORDER BY u.username
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT udp.*, u.username, usd.mountpoint AS zfs_mountpoint, usd.status AS zfs_status
                    FROM user_data_policies udp
                    JOIN users u ON u.id = udp.user_id
                    LEFT JOIN user_storage_datasets usd ON usd.user_id = udp.user_id
                    WHERE udp.user_id = %s
                    """
                    , (actor["id"],),
                ).fetchall()
            return rows

    def require_user_directory_access(conn, user_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        actor = current_user(conn)
        if not is_admin_user(actor) and actor["id"] != user_id:
            raise HTTPException(status_code=403, detail="只能浏览自己的用户目录")
        user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        return actor, user, ensure_user_data_policy(conn, user)

    @app.get("/api/storage/users/{user_id}/files")
    def user_directory_files(user_id: int, relative_path: str = ""):
        relative_path = normalize_relative_directory(relative_path)
        with db() as conn:
            require_user_directory_access(conn, user_id)
            row = conn.execute(
                "SELECT * FROM user_directory_scans WHERE user_id = %s AND relative_path = %s",
                (user_id, relative_path),
            ).fetchone()
            return row or {
                "user_id": user_id,
                "relative_path": relative_path,
                "status": "unknown",
                "file_count": 0,
                "size_bytes": 0,
                "entries": [],
                "truncated": False,
                "error": "",
                "scanned_at": 0,
            }

    @app.get("/api/storage/users/{user_id}/files/live")
    async def user_directory_live(user_id: int, relative_path: str = ""):
        """即时返回文件列表：优先通过 agent HTTP API（< 10ms），回退到 SSH ls。"""
        relative_path = normalize_relative_directory(relative_path)
        dataset_name = ""
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            # 查询 ZFS 数据集名称，用于获取实际用量
            dataset_row = conn.execute(
                "SELECT dataset_name FROM user_storage_datasets WHERE user_id = %s AND node_id = %s",
                (user_id, node["id"]),
            ).fetchone()
            if dataset_row:
                dataset_name = dataset_row["dataset_name"] or ""
            audit(conn, actor["username"], "live-ls", f"user-directory:{user_id}", {"path": relative_path})

        node_id = int(node["id"])

        # ── 优先：agent HTTP API（无 SSH 开销，毫秒级响应）──────────────────────
        # 仅在根目录且 ZFS 缓存未命中时才传入 dataset_name 让 agent 查询
        zfs_for_agent = ""
        if not relative_path and dataset_name:
            cached = _cached_zfs_usage(node_id, dataset_name)
            if cached is None:
                zfs_for_agent = dataset_name
        agent_result = await _list_via_agent_http(node, absolute_path, root_path, zfs_dataset=zfs_for_agent)
        if agent_result is not None:
            size_bytes = agent_result.get("size_bytes", 0)
            # 更新 ZFS 用量缓存（若 agent 已查询过）
            if not relative_path and dataset_name and zfs_for_agent:
                _store_zfs_usage(node_id, dataset_name, size_bytes)
            elif not relative_path and dataset_name:
                cached = _cached_zfs_usage(node_id, dataset_name)
                if cached is not None:
                    size_bytes = cached
            return {
                "user_id": user_id,
                "relative_path": relative_path,
                "status": agent_result.get("status", "ready"),
                "file_count": agent_result.get("file_count", 0),
                "size_bytes": size_bytes,
                "entries": agent_result.get("entries", []),
                "truncated": agent_result.get("truncated", False),
                "error": agent_result.get("error", ""),
                "scanned_at": int(time.time()),
            }

        # ── 回退：SSH ls -la（兼容无 agent HTTP 的旧节点）──────────────────────
        async def _run(ssh) -> dict[str, Any]:
            # ls -la --time-style=+%s 输出: drwxr-xr-x 2 root root 4096 1719876543 dirname
            result = await ssh.run(
                f"ls -la --time-style=+%s {shlex.quote(absolute_path)}",
                check=False,
            )
            if result.exit_status != 0:
                err = (result.stderr or "").strip()
                # 目录不存在 -> 返回 empty-ready（允许页面正常渲染，entries 为空）
                if "cannot access" in err.lower() or "no such file" in err.lower():
                    return {
                        "user_id": user_id,
                        "relative_path": relative_path,
                        "status": "ready",
                        "file_count": 0,
                        "size_bytes": 0,
                        "entries": [],
                        "truncated": False,
                        "error": f"目录不存在: {absolute_path}",
                        "scanned_at": int(time.time()),
                    }
                raise HTTPException(status_code=500, detail=f"ls 执行失败: {err[:200]}")
            lines = (result.stdout or "").strip().split("\n")
            entries = []
            total_size = 0
            file_count = 0
            for line in lines:
                line = line.strip()
                if not line or line.startswith("total "):
                    continue
                # ls -la --time-style=+%s 输出格式:
                # perms links owner group size mtime_epoch filename
                # 使用 split(None, 6) 将前6个字段分割，其余作为文件名（支持空格）
                parts = line.split(None, 6)
                if len(parts) < 7:
                    continue
                # parts: [perms, links, owner, group, size, mtime_epoch, name]
                name = parts[6]
                if name in (".", ".."):
                    continue
                perms = parts[0]
                entry_type = "directory" if perms.startswith("d") else "symlink" if perms.startswith("l") else "file"
                try:
                    size_bytes = int(parts[4])
                except ValueError:
                    size_bytes = 0
                try:
                    mtime = int(parts[5])
                except ValueError:
                    mtime = 0
                entries.append({
                    "name": name,
                    "type": entry_type,
                    "size_bytes": size_bytes,
                    "mtime": mtime,
                    "mode": perms,
                })
                if entry_type != "directory":
                    total_size += size_bytes
                    file_count += 1
            # 如果是根目录且有 ZFS 数据集，使用 zfs get 获取实际用量（带 TTL 缓存）
            if not relative_path and dataset_name:
                cached = _cached_zfs_usage(node_id, dataset_name)
                if cached is not None:
                    total_size = cached
                else:
                    try:
                        zfs_cmd = f"zfs get -H -o value used {shlex.quote(dataset_name)}"
                        zfs_result = await ssh.run(zfs_cmd, check=False)
                        if zfs_result.exit_status == 0:
                            zfs_used = (zfs_result.stdout or "").strip()
                            if zfs_used and zfs_used != "-":
                                try:
                                    # zfs get 返回的可能带单位（如 1.5G），需要解析
                                    total_size = parse_zfs_size(zfs_used)
                                    _store_zfs_usage(node_id, dataset_name, total_size)
                                except (ValueError, TypeError):
                                    pass
                    except (asyncssh.Error, OSError):
                        # zfs 命令失败不影响文件列表返回，沿用 ls 累加的大小
                        pass
            return {
                "user_id": user_id,
                "relative_path": relative_path,
                "status": "ready",
                "file_count": file_count,
                "size_bytes": total_size,
                "entries": entries,
                "truncated": False,
                "error": "",
                "scanned_at": int(time.time()),
            }

        try:
            return await _with_pooled_ssh(node, _run)
        except (OSError, asyncssh.Error) as exc:
            raise HTTPException(status_code=500, detail=f"SSH 连接存储节点失败: {str(exc)[:200]}")

    @app.post("/api/storage/users/{user_id}/files/scan", status_code=202)
    def scan_user_directory(user_id: int, payload: UserDirectoryScanInput):
        relative_path = normalize_relative_directory(payload.relative_path)
        limit = max(1, min(payload.limit, 1000))
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            ts = now_ts()
            conn.execute(
                """
                INSERT INTO user_directory_scans (
                    user_id, relative_path, node_id, absolute_path, status, updated_at
                ) VALUES (%s, %s, %s, %s, 'scanning', %s)
                ON CONFLICT (user_id, relative_path) DO UPDATE SET
                    node_id = EXCLUDED.node_id, absolute_path = EXCLUDED.absolute_path,
                    status = 'scanning', error = '', updated_at = EXCLUDED.updated_at
                """,
                (user_id, relative_path, node["id"], absolute_path, ts),
            )
            task = enqueue_node_task(
                conn,
                node["id"],
                None,
                "scan_user_directory",
                {
                    "user_id": user_id,
                    "username": user["username"],
                    "relative_path": relative_path,
                    "root_path": root_path,
                    "path": absolute_path,
                    "limit": limit,
                },
            )
            audit(conn, actor["username"], "scan", f"user-directory:{user_id}", {"path": relative_path, "node": node["hostname"]})
            return public_task(task)

    @app.post("/api/storage/users/{user_id}/upload")
    async def user_directory_upload(
        user_id: int,
        relative_path: str = Form(""),
        files: list[UploadFile] = File(...),
        paths: list[str] = Form(...),
    ):
        relative_path = normalize_relative_directory(relative_path)
        if not files:
            raise HTTPException(status_code=400, detail="请选择要上传的文件")
        if len(files) != len(paths):
            raise HTTPException(status_code=400, detail="上传文件与路径数量不一致")
        max_bytes = max(1, MAX_UPLOAD_MB) * 1024 * 1024
        known_sizes = [getattr(upload, "size", None) for upload in files]
        if all(isinstance(size, int) and size >= 0 for size in known_sizes) and sum(known_sizes) > max_bytes:
            raise HTTPException(status_code=413, detail=f"上传总大小超过限制（{MAX_UPLOAD_MB} MB）")
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            quota = conn.execute("SELECT storage_quota_gb FROM quotas WHERE user_id = %s", (user_id,)).fetchone()
            storage_quota_gb = int((quota or {}).get("storage_quota_gb") or 0)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            target_root = f"{root_path}/{relative_path}" if relative_path else root_path
            if not (target_root == root_path or target_root.startswith(root_path.rstrip("/") + "/")):
                raise HTTPException(status_code=400, detail="上传路径越界")
            audit(conn, actor["username"], "upload", f"user-directory:{user_id}", {"path": relative_path, "files": len(files)})

        uploaded_count = 0
        uploaded_bytes = 0
        bandwidth_limit_mbps = _transfer_bandwidth_limit_mbps()
        snapshot: dict[str, Any] | None = None
        temp_suffix = f".uploading-{int(time.time())}"
        try:
            async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
                quota_bytes = storage_quota_gb * 1024 * 1024 * 1024
                current_bytes = 0
                if quota_bytes > 0:
                    result = await ssh.run(
                        f"test -e {shlex.quote(root_path)} && du -sb {shlex.quote(root_path)} | awk '{{print $1}}' || echo 0",
                        check=False,
                    )
                    try:
                        current_bytes = int((result.stdout or "0").strip().splitlines()[-1] or "0")
                    except (ValueError, IndexError):
                        current_bytes = 0
                    if all(isinstance(size, int) and size >= 0 for size in known_sizes):
                        if current_bytes + sum(known_sizes) > quota_bytes:
                            raise HTTPException(status_code=413, detail=f"用户目录超过存储上限（{storage_quota_gb} GB）")
                await ssh.run(f"mkdir -p {shlex.quote(target_root)}", check=True)
                async with ssh.start_sftp_client() as sftp:
                    for index, upload in enumerate(files):
                        upload_rel = normalize_upload_relative_path(paths[index] or upload.filename or "")
                        remote_path = posixpath.join(target_root, upload_rel)
                        if not remote_path.startswith(root_path.rstrip("/") + "/"):
                            raise HTTPException(status_code=400, detail="上传路径越界")
                        remote_parent = posixpath.dirname(remote_path) or "/"
                        await ssh.run(f"mkdir -p {shlex.quote(remote_parent)}", check=True)
                        is_existing_dir = (await ssh.run(f"test -d {shlex.quote(remote_path)}", check=False)).exit_status == 0
                        if is_existing_dir:
                            raise HTTPException(status_code=400, detail=f"目标已存在同名目录：{upload_rel}")
                        temp_remote = f"{remote_path}{temp_suffix}-{index}"
                        try:
                            async with sftp.open(temp_remote, "wb") as remote_file:
                                while True:
                                    chunk = await upload.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    uploaded_bytes += len(chunk)
                                    if uploaded_bytes > max_bytes:
                                        raise HTTPException(status_code=413, detail=f"上传总大小超过限制（{MAX_UPLOAD_MB} MB）")
                                    if quota_bytes > 0 and current_bytes + uploaded_bytes > quota_bytes:
                                        raise HTTPException(status_code=413, detail=f"用户目录超过存储上限（{storage_quota_gb} GB）")
                                    await _throttle_transfer_chunk(len(chunk), bandwidth_limit_mbps)
                                    await remote_file.write(chunk)
                            await ssh.run(f"mv -f {shlex.quote(temp_remote)} {shlex.quote(remote_path)}", check=True)
                            uploaded_count += 1
                        except Exception:
                            await ssh.run(f"rm -f {shlex.quote(temp_remote)}", check=False)
                            raise
                        finally:
                            await upload.close()
                    usage_result = await ssh.run(
                        f"test -e {shlex.quote(root_path)} && du -sb {shlex.quote(root_path)} | awk '{{print $1}}' || echo 0",
                        check=False,
                    )
                    try:
                        current_bytes = int((usage_result.stdout or "0").strip().splitlines()[-1] or "0")
                    except (ValueError, IndexError):
                        current_bytes = current_bytes + uploaded_bytes
                    snapshot = await _sftp_directory_snapshot(
                        sftp,
                        target_root,
                        user_id,
                        relative_path,
                        500,
                        current_bytes if not relative_path else uploaded_bytes,
                    )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"上传到存储节点失败：{exc}") from exc

        scan_prefix = f"{relative_path}/%" if relative_path else "%"
        with db() as conn:
            if relative_path:
                conn.execute(
                    "DELETE FROM user_directory_scans WHERE user_id = %s AND (relative_path = %s OR relative_path LIKE %s)",
                    (user_id, relative_path, scan_prefix),
                )
            else:
                conn.execute("DELETE FROM user_directory_scans WHERE user_id = %s", (user_id,))
            if snapshot:
                ts = now_ts()
                conn.execute(
                    """
                    INSERT INTO user_directory_scans (
                        user_id, relative_path, node_id, absolute_path, status, file_count, size_bytes,
                        entries, truncated, error, scanned_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, relative_path) DO UPDATE SET
                        node_id = EXCLUDED.node_id,
                        absolute_path = EXCLUDED.absolute_path,
                        status = EXCLUDED.status,
                        file_count = EXCLUDED.file_count,
                        size_bytes = EXCLUDED.size_bytes,
                        entries = EXCLUDED.entries,
                        truncated = EXCLUDED.truncated,
                        error = EXCLUDED.error,
                        scanned_at = EXCLUDED.scanned_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user_id,
                        relative_path,
                        node["id"],
                        target_root,
                        snapshot["status"],
                        snapshot["file_count"],
                        snapshot["size_bytes"],
                        Jsonb(snapshot["entries"]),
                        snapshot["truncated"],
                        snapshot["error"],
                        snapshot["scanned_at"],
                        ts,
                    ),
                )
        return {"ok": True, "count": uploaded_count, "bytes": uploaded_bytes, "scan": snapshot}

    @app.get("/api/storage/users/{user_id}/download-info")
    def user_directory_download_info(user_id: int, relative_path: str = ""):
        relative_path = normalize_relative_directory(relative_path)
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            command = f"scp -r {user['username']}@{node['ip']}:'{absolute_path}' ."
            audit(conn, actor["username"], "download-info", f"user-directory:{user_id}", {"path": relative_path})
            return {"node": node["hostname"], "host": node["ip"], "path": absolute_path, "command": command}

    @app.get("/api/storage/users/{user_id}/download")
    async def user_directory_download(user_id: int, relative_path: str = ""):
        relative_path = normalize_relative_directory(relative_path)
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            audit(conn, actor["username"], "download", f"user-directory:{user_id}", {"path": relative_path})

        try:
            async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
                exists = (await ssh.run(f"test -e {shlex.quote(absolute_path)}", check=False)).exit_status == 0
                if not exists:
                    raise HTTPException(status_code=404, detail="下载目标不存在")
                is_dir = (await ssh.run(f"test -d {shlex.quote(absolute_path)}", check=False)).exit_status == 0
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"无法连接存储节点执行下载：{exc}") from exc

        filename = _download_filename(relative_path or os.path.basename(policy["home_path"].rstrip("/")), is_dir)
        headers = {"Content-Disposition": _content_disposition(filename)}
        bandwidth_limit_mbps = _transfer_bandwidth_limit_mbps()

        if not is_dir:
            async def file_stream():
                async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
                    async with ssh.start_sftp_client() as sftp:
                        async with sftp.open(absolute_path, "rb") as remote_file:
                            while True:
                                chunk = await remote_file.read(_TRANSFER_CHUNK_SIZE)
                                if not chunk:
                                    break
                                await _throttle_transfer_chunk(len(chunk), bandwidth_limit_mbps)
                                yield chunk

            return StreamingResponse(file_stream(), media_type="application/octet-stream", headers=headers)

        archive_name = os.path.basename(absolute_path.rstrip("/")) or os.path.basename(root_path.rstrip("/")) or "home"

        async def archive_stream():
            async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
                async with ssh.start_sftp_client() as sftp:
                    file_tree = await _sftp_collect_tree(sftp, absolute_path, archive_name)
                    buf = io.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                        for sftp_path, arc_path, is_dir, size, mtime in file_tree:
                            info = tarfile.TarInfo(name=arc_path)
                            info.mtime = mtime
                            if is_dir:
                                info.type = tarfile.DIRTYPE
                                info.mode = 0o755
                                tf.addfile(info)
                            else:
                                info.size = size
                                info.mode = 0o644
                                async with await sftp.open(sftp_path, "rb") as f:
                                    file_bytes = await f.read()
                                tf.addfile(info, io.BytesIO(file_bytes))
            data = buf.getvalue()
            chunk_size = 1 << 20
            for i in range(0, len(data), chunk_size):
                chunk = data[i : i + chunk_size]
                await _throttle_transfer_chunk(len(chunk), bandwidth_limit_mbps)
                yield chunk

        return StreamingResponse(archive_stream(), media_type="application/gzip", headers=headers)

    @app.get("/api/storage/users/{user_id}/preview")
    async def user_directory_preview(user_id: int, relative_path: str):
        relative_path = normalize_relative_directory(relative_path)
        if not relative_path:
            raise HTTPException(status_code=400, detail="只能预览具体文件")
        node_ref: dict[str, Any] | str = ""
        absolute_path = ""
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            node_ref = node
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            audit(conn, actor["username"], "preview", f"user-directory:{user_id}", {"path": relative_path})
        try:
            preview = await _preview_shared_resource_file(node_ref, absolute_path)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"文件预览失败：{exc}") from exc
        preview.update({
            "name": os.path.basename(relative_path),
            "relative_path": relative_path,
        })
        return preview

    @app.delete("/api/storage/users/{user_id}/file", status_code=204)
    async def user_directory_delete(user_id: int, relative_path: str):
        relative_path = normalize_relative_directory(relative_path)
        if not relative_path:
            raise HTTPException(status_code=400, detail="不能删除用户根目录")
        with db() as conn:
            actor, user, policy = require_user_directory_access(conn, user_id)
            node, root_path = _resolve_user_directory_root(conn, user_id, policy)
            absolute_path = f"{root_path}/{relative_path}"
            # 安全检查：删除路径必须在 home 根目录以下
            if not absolute_path.startswith(root_path.rstrip("/") + "/"):
                raise HTTPException(status_code=400, detail="删除路径越界")
            audit(conn, actor["username"], "delete", f"user-directory:{user_id}", {"path": relative_path})
        try:
            async with asyncssh.connect(**_shared_resource_ssh_kwargs(node)) as ssh:
                exists = (await ssh.run(f"test -e {shlex.quote(absolute_path)}", check=False)).exit_status == 0
                if not exists:
                    raise HTTPException(status_code=404, detail="文件或目录不存在")
                result = await ssh.run(f"rm -rf {shlex.quote(absolute_path)}", check=False)
                if result.exit_status != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    raise HTTPException(status_code=500, detail=f"删除失败：{err}")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"无法连接存储节点执行删除：{exc}") from exc
        # 清除父目录及当前路径的扫描缓存，使下次浏览触发重新扫描
        parent_rel = "/".join(relative_path.split("/")[:-1])
        with db() as conn:
            conn.execute(
                "DELETE FROM user_directory_scans WHERE user_id = %s AND relative_path IN (%s, %s)",
                (user_id, relative_path, parent_rel),
            )

    @app.get("/api/data/shared-resources")
    def shared_resources():
        with db() as conn:
            rows = conn.execute("SELECT * FROM shared_resources ORDER BY resource_type, name, version").fetchall()
            return [public_shared_resource(row) for row in rows]

    @app.post("/api/data/shared-resources", status_code=201)
    def create_shared_resource(payload: SharedResourceInput):
        require_admin()
        payload = normalize_shared_resource(payload)
        ts = now_ts()
        with db() as conn:
            row = conn.execute(
                """
                INSERT INTO shared_resources (
                    resource_type, name, version, source_path, mount_path, tags, readonly, sync_policy, enabled, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (resource_type, name, version) DO UPDATE SET
                    source_path = EXCLUDED.source_path,
                    mount_path = EXCLUDED.mount_path,
                    tags = EXCLUDED.tags,
                    readonly = EXCLUDED.readonly,
                    sync_policy = EXCLUDED.sync_policy,
                    enabled = EXCLUDED.enabled,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    payload.resource_type,
                    payload.name,
                    payload.version,
                    payload.source_path,
                    payload.mount_path,
                    payload.tags,
                    payload.readonly,
                    payload.sync_policy,
                    payload.enabled,
                    ts,
                    ts,
                ),
            ).fetchone()
            audit(conn, "admin", "upsert", f"shared-resource:{row['id']}", payload.model_dump())
            return public_shared_resource(row)

    @app.put("/api/data/shared-resources/{resource_id}")
    def update_shared_resource(resource_id: int, payload: SharedResourceInput):
        require_admin()
        payload = normalize_shared_resource(payload)
        ts = now_ts()
        with db() as conn:
            row = conn.execute(
                """
                UPDATE shared_resources
                SET resource_type = %s,
                    name = %s,
                    version = %s,
                    source_path = %s,
                    mount_path = %s,
                    tags = %s,
                    readonly = %s,
                    sync_policy = %s,
                    enabled = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    payload.resource_type,
                    payload.name,
                    payload.version,
                    payload.source_path,
                    payload.mount_path,
                    payload.tags,
                    payload.readonly,
                    payload.sync_policy,
                    payload.enabled,
                    ts,
                    resource_id,
                ),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            audit(conn, "admin", "update", f"shared-resource:{resource_id}", payload.model_dump())
            return public_shared_resource(row)

    @app.patch("/api/data/shared-resources/{resource_id}/info")
    def update_shared_resource_info(resource_id: int, payload: SharedResourceInfoInput):
        require_admin()
        name = payload.name.strip()
        version = payload.version.strip() or "default"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", name):
            raise HTTPException(status_code=400, detail="资源名称不合法")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,80}", version):
            raise HTTPException(status_code=400, detail="资源提供者不合法")
        tags = normalize_shared_resource_tags(payload.tags)
        ts = now_ts()
        with db() as conn:
            actor = current_user(conn)
            upsert_tag_options(conn, tags)
            row = conn.execute(
                """UPDATE shared_resources SET name=%s, version=%s, tags=%s, updated_at=%s
                   WHERE id=%s RETURNING *""",
                (name, version, tags, ts, resource_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            audit(conn, actor["username"], "update-info", f"shared-resource:{resource_id}",
                  {"name": name, "version": version, "tags": tags})
            return public_shared_resource(row)

    @app.put("/api/data/shared-resources/{resource_id}/tags")
    def update_shared_resource_tags(resource_id: int, payload: SharedResourceTagsInput):
        require_admin()
        tags = normalize_shared_resource_tags(payload.tags)
        ts = now_ts()
        with db() as conn:
            upsert_tag_options(conn, tags)
            row = conn.execute(
                """
                UPDATE shared_resources
                SET tags = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (tags, ts, resource_id),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            audit(conn, "admin", "update", f"shared-resource-tags:{resource_id}", {"tags": tags})
            return public_shared_resource(row)

    @app.post("/api/data/shared-resources/{resource_id}/verify", status_code=202)
    def verify_shared_resource(resource_id: int):
        require_admin()
        with db() as conn:
            current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            node = select_storage_node_for_path(conn, resource["source_path"])
            if not node:
                raise HTTPException(status_code=400, detail="没有 online 的 storage/mixed 节点可执行校验")
            source_path = source_path_for_node(resource["source_path"], node)
            task = enqueue_shared_resource_verify_task(conn, resource, node, source_path)
            audit(conn, "admin", "verify", f"shared-resource:{resource_id}", {"node": node["hostname"], "path": source_path})
            return public_task(task)

    @app.post("/api/data/resource-requests", status_code=202)
    async def request_shared_resource(payload: SharedResourceRequestInput):
        actor = current_user(None)
        name = payload.name.strip()
        version = payload.version.strip() or "default"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,80}", name):
            raise HTTPException(status_code=400, detail="资源名称不合法")
        source = payload.source.strip().lower() or "huggingface"
        if source not in ("huggingface", "modelscope"):
            raise HTTPException(status_code=400, detail="source 不合法，应为 huggingface 或 modelscope")
        repo_type = "dataset" if payload.resource_type == "dataset" else "model"
        tags = normalize_shared_resource_tags(payload.tags)

        with db() as _tc:
            upsert_tag_options(_tc, tags)

        if source == "modelscope":
            ms_repo_id = payload.ms_repo_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}", ms_repo_id):
                raise HTTPException(status_code=400, detail="ModelScope 仓库 ID 不合法，格式应为 owner/repo-name")
            ms_revision = re.sub(r"[^\w./-]", "", payload.ms_revision.strip()) or "master"
            ms_token = payload.ms_token.strip()
            source_url = f"ms://{ms_repo_id}@{ms_revision}"
        else:
            hf_repo_id = payload.hf_repo_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}", hf_repo_id):
                raise HTTPException(status_code=400, detail="HuggingFace 仓库 ID 不合法，格式应为 owner/repo-name")
            hf_revision = re.sub(r"[^\w./-]", "", payload.hf_revision.strip()) or "main"
            hf_token = payload.hf_token.strip()
            source_url = f"hf://{hf_repo_id}@{hf_revision}"

        with db() as conn:
            settings = get_storage_settings(conn)
            hf_endpoint = settings["hf_endpoint"] if settings["hf_endpoint_enabled"] == "1" else ""
            if payload.resource_type == "dataset":
                base = settings["dataset_base_path"].rstrip("/")
                mount_path = f"/datasets/{name}"
            else:
                base = settings["model_base_path"].rstrip("/")
                mount_path = f"/models/{name}"
            platform_path = f"{base}/{name}/{version}"
            node = select_storage_node_for_path(conn, platform_path)
            if not node:
                raise HTTPException(status_code=400, detail="没有在线存储节点")
            ts = now_ts()
            row = conn.execute(
                     """INSERT INTO shared_resources (resource_type,name,version,source_path,mount_path,tags,readonly,sync_policy,enabled,source_url,request_status,requested_by,created_at,updated_at)
                         VALUES (%s,%s,%s,%s,%s,%s,TRUE,'manual',TRUE,%s,'downloading',%s,%s,%s)
                         ON CONFLICT (resource_type,name,version) DO UPDATE SET source_url=EXCLUDED.source_url,tags=EXCLUDED.tags,request_status='downloading',requested_by=EXCLUDED.requested_by,updated_at=EXCLUDED.updated_at RETURNING *""",
                     (payload.resource_type, name, version, platform_path, mount_path, tags, source_url, actor["id"], ts, ts),
            ).fetchone()
            target = source_path_for_node(platform_path, node)
            audit(conn, actor["username"], f"request-{source}-download", f"shared-resource:{row['id']}",
                  {"repo_id": ms_repo_id if source == "modelscope" else hf_repo_id})

        # 在后端事件循环中启动后台下载任务（不阻塞请求）
        if source == "modelscope":
            asyncio.create_task(
                run_queued_shared_resource_download(
                    row["id"],
                    lambda: backend_ms_download_and_sync(
                        row["id"],
                        ms_repo_id,
                        ms_revision,
                        ms_token,
                        repo_type,
                        node,
                        target,
                    ),
                )
            )
        else:
            asyncio.create_task(
                run_queued_shared_resource_download(
                    row["id"],
                    lambda: backend_hf_download_and_sync(
                        row["id"],
                        hf_repo_id,
                        hf_revision,
                        hf_token,
                        hf_endpoint,
                        repo_type,
                        node,
                        target,
                    ),
                )
            )
        return {"resource": public_shared_resource(row)}

    @app.get("/api/data/shared-resources/{resource_id}/files")
    def shared_resource_files(resource_id: int, relative_path: str = ""):
        relative_path = normalize_relative_directory(relative_path)
        with db() as conn:
            current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            row = conn.execute(
                "SELECT * FROM shared_resource_scans WHERE resource_id = %s AND relative_path = %s",
                (resource_id, relative_path),
            ).fetchone()
            return row or {
                "resource_id": resource_id,
                "relative_path": relative_path,
                "status": "unknown",
                "file_count": 0,
                "size_bytes": 0,
                "entries": [],
                "truncated": False,
                "error": "",
                "scanned_at": 0,
            }

    @app.post("/api/data/shared-resources/{resource_id}/files/scan", status_code=202)
    def scan_shared_resource(resource_id: int, relative_path: str = ""):
        relative_path = normalize_relative_directory(relative_path)
        with db() as conn:
            actor = current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            node = select_storage_node_for_path(conn, resource["source_path"])
            if not node:
                raise HTTPException(status_code=400, detail="没有 online 的 storage/mixed 节点可扫描资源目录")
            root_path = source_path_for_node(resource["source_path"], node)
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
            ts = now_ts()
            conn.execute(
                """
                INSERT INTO shared_resource_scans (
                    resource_id, relative_path, node_id, absolute_path, status, updated_at
                ) VALUES (%s, %s, %s, %s, 'scanning', %s)
                ON CONFLICT (resource_id, relative_path) DO UPDATE SET
                    node_id = EXCLUDED.node_id, absolute_path = EXCLUDED.absolute_path,
                    status = 'scanning', error = '', updated_at = EXCLUDED.updated_at
                """,
                (resource_id, relative_path, node["id"], absolute_path, ts),
            )
            task = enqueue_node_task(
                conn,
                node["id"],
                None,
                "scan_shared_resource",
                {
                    "resource_id": resource_id,
                    "relative_path": relative_path,
                    "root_path": root_path,
                    "path": absolute_path,
                    "limit": 500,
                },
            )
            audit(conn, actor["username"], "scan", f"shared-resource:{resource_id}", {"path": relative_path, "node": node["hostname"]})
            return public_task(task)

    @app.get("/api/data/shared-resources/{resource_id}/download-info")
    def shared_resource_download_info(resource_id: int):
        with db() as conn:
            actor = current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            node = select_storage_node_for_path(conn, resource["source_path"])
            if not node:
                raise HTTPException(status_code=400, detail="资源所在存储节点不在线")
            absolute_path = source_path_for_node(resource["source_path"], node)
            command = f"scp -r {node['hostname']}:'{absolute_path}' ."
            audit(conn, actor["username"], "download-info", f"shared-resource:{resource_id}", {"path": absolute_path})
            return {"node": node["hostname"], "host": node["ip"], "path": absolute_path, "command": command}

    @app.get("/api/data/shared-resources/{resource_id}/preview")
    async def shared_resource_preview(resource_id: int, relative_path: str):
        relative_path = normalize_relative_directory(relative_path)
        if not relative_path:
            raise HTTPException(status_code=400, detail="只能预览具体文件")
        node_ref: dict[str, Any] | str = ""
        with db() as conn:
            current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            node = select_storage_node_for_path(conn, resource["source_path"])
            if not node:
                raise HTTPException(status_code=400, detail="资源所在存储节点不在线")
            root_path = source_path_for_node(resource["source_path"], node)
            node_ref = node
            absolute_path = f"{root_path}/{relative_path}" if relative_path else root_path
        try:
            preview = await _preview_shared_resource_file(node_ref, absolute_path)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"文件预览失败：{exc}") from exc
        preview.update({
            "name": os.path.basename(relative_path),
            "relative_path": relative_path,
        })
        with db() as conn:
            audit(conn, "system", "preview", f"shared-resource:{resource_id}", {"path": relative_path, "kind": preview.get("kind", "unknown")})
        return preview

    @app.delete("/api/data/shared-resources/{resource_id}", status_code=204)
    def delete_shared_resource(resource_id: int):
        require_admin()
        with db() as conn:
            actor = current_user(conn)
            resource = conn.execute("SELECT * FROM shared_resources WHERE id = %s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(status_code=404, detail="共享资源不存在")
            conn.execute("DELETE FROM shared_resources WHERE id = %s", (resource_id,))
            audit(conn, actor["username"], "delete", f"shared-resource:{resource_id}", {"name": resource["name"], "version": resource["version"]})
        # 清理该资源的后端暂存目录（断点续下缓存）
        for _prefix in ("hf-", "ms-"):
            _staging = os.path.join(HF_STAGING_DIR, f"{_prefix}{resource_id}")
            if os.path.isdir(_staging):
                shutil.rmtree(_staging, ignore_errors=True)
