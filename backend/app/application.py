import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .agent.routes import register_agent_routes
from .agent.updates import register_agent_update_routes
from .auth import authenticate_token, bearer_token, request_user
from .auth_routes import register_auth_routes
from .sso.routes import register_sso_routes
from .agent.tasks import enqueue_node_task, public_task, verify_agent_node
from .config import CORS_ALLOW_ORIGINS
from .containers.ports import add_container_port, incus_ports_payload, list_container_ports, normalize_port_payload
from .containers.routes import register_container_routes
from .containers.scheduler import select_node_and_gpus
from .containers.services import (
    build_data_mounts,
    incus_create_payload,
    incus_lifecycle_payload,
    list_containers,
    usage_for_user,
)
from .core import audit, current_user, db, hash_token, now_ts, validate_platform_path
from .data.routes import register_data_routes
from .database import init_schema
from .images.routes import register_image_routes
from .internal.routes import register_internal_routes
from .metrics.routes import register_metrics_routes
from .nodes.routes import register_node_routes
from .settings.routes import register_settings_routes
from .nodes.services import get_node, gpu_with_container, mark_stale_nodes, upsert_node
from .storage.routes import register_storage_routes
from .storage.services import (
    enqueue_incus_image_import_task,
    ensure_user_zfs_dataset_task,
    find_node_incus_image,
    incus_image_import_payload,
    node_has_incus_image,
    public_storage_image_file,
    remove_user_workspace_volume_task,
    remove_user_zfs_dataset_task,
    select_storage_node_for_path,
    storage_image_base_name,
    storage_root_for_node,
)
from .users.routes import register_user_routes


def create_app() -> FastAPI:
    app = FastAPI(title="GPU Cluster Platform", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    public_api_paths = {"/api/health", "/api/auth/config", "/api/auth/login", "/api/auth/register", "/api/auth/sso/providers", "/api/auth/sso/callback", "/api/nodes/register"}

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in public_api_paths or path.startswith("/api/internal/") \
                or path.startswith("/api/auth/sso/start/") \
                or path.startswith("/api/agent-updates/") \
                or path.startswith("/api/nodes/tasks/") or (path.startswith("/api/nodes/") and path.endswith("/heartbeat")):
            return await call_next(request)
        with db() as conn:
            user = authenticate_token(conn, bearer_token(request), now_ts())
        if not user:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        context = request_user.set(user)
        try:
            return await call_next(request)
        finally:
            request_user.reset(context)

    @app.on_event("startup")
    async def startup():
        init_schema()
        ts = now_ts()
        with db() as conn:
            # 清理上次服务重启前未完成的后端下载任务（避免永久卡在 downloading）
            conn.execute(
                "UPDATE shared_resources SET request_status='failed', "
                "check_error='服务重启，请重新提交下载', updated_at=%s "
                "WHERE request_status='downloading'",
                (ts,),
            )
        # 每日清理过期会话行，防止 auth_sessions 表无限增长
        async def _session_cleanup_loop():
            while True:
                try:
                    await asyncio.sleep(86400)  # 每 24 小时运行一次
                    with db() as conn:
                        deleted = conn.execute(
                            "DELETE FROM auth_sessions WHERE expires_at < %s",
                            (now_ts(),),
                        ).rowcount
                        if deleted:
                            print(f"[INFO] session cleanup: deleted {deleted} expired rows", flush=True)
                except Exception as exc:
                    import sys
                    print(f"[WARN] session cleanup error: {exc!r}", file=sys.stderr, flush=True)
        asyncio.create_task(_session_cleanup_loop())
        asyncio.create_task(_webhook_alert_loop())

    async def _webhook_alert_loop():
        """每 60 秒检查告警，有新告警时向 webhook_url POST 通知（best-effort）。"""
        import httpx
        from .metrics.routes import register_metrics_routes  # noqa — just for _build_alerts reference
        _seen: set[str] = set()
        while True:
            try:
                await asyncio.sleep(60)
                with db() as conn:
                    from .platform_settings import get_platform_settings, setting_to_bool
                    settings = get_platform_settings(conn)
                    if not setting_to_bool(settings.get("webhook_enabled")):
                        _seen.clear()
                        continue
                    webhook_url = (settings.get("webhook_url") or "").strip()
                    if not webhook_url:
                        continue
                    from .nodes.services import get_node, mark_stale_nodes
                    from .containers.services import list_containers
                    mark_stale_nodes(conn)
                    node_rows = conn.execute("SELECT * FROM nodes ORDER BY hostname").fetchall()
                    all_nodes = [get_node(conn, r["id"]) for r in node_rows]
                    all_containers = list_containers(conn)
                    from .metrics.routes import _build_alerts_standalone
                    alerts = _build_alerts_standalone(conn, all_nodes, all_containers)
                    new_alerts = [a for a in alerts if f"{a['type']}:{a.get('node_id')}" not in _seen]
                    _seen.clear()
                    _seen.update(f"{a['type']}:{a.get('node_id')}" for a in alerts)
                    if not new_alerts:
                        continue
                webhook_secret = (settings.get("webhook_secret") or "").strip()
                headers = {"Content-Type": "application/json"}
                if webhook_secret:
                    headers["X-Webhook-Secret"] = webhook_secret
                payload_body = {"alerts": new_alerts, "timestamp": now_ts()}
                async with httpx.AsyncClient(timeout=8) as client:
                    await client.post(webhook_url, json=payload_body, headers=headers)
            except Exception as exc:
                import sys
                print(f"[WARN] webhook_alert_loop: {exc!r}", file=sys.stderr, flush=True)


    register_user_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "current_user": current_user,
            "usage_for_user": usage_for_user,
            "enqueue_node_task": enqueue_node_task,
            "ensure_user_zfs_dataset_task": lambda conn, user_id, reason="ensure": ensure_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
                reason=reason,
            ),
        },
    )
    register_auth_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "current_user": current_user,
            "ensure_user_zfs_dataset_task": lambda conn, user_id, reason="platform-register": ensure_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
                reason=reason,
            ),
        },
    )
    register_settings_routes(app, {"db": db, "now_ts": now_ts, "audit": audit})
    register_sso_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "ensure_user_zfs_dataset_task": lambda conn, user_id, reason="sso": ensure_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
                reason=reason,
            ),
        },
    )
    register_node_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "audit": audit,
            "hash_token": hash_token,
            "mark_stale_nodes": mark_stale_nodes,
            "get_node": get_node,
            "gpu_with_container": gpu_with_container,
            "enqueue_node_task": enqueue_node_task,
            "public_task": public_task,
            "current_user": current_user,
        },
    )
    register_agent_update_routes(app, {"db": db, "now_ts": now_ts, "audit": audit, "verify_agent_node": verify_agent_node})
    register_image_routes(app, {"db": db, "now_ts": now_ts, "audit": audit})
    register_data_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "audit": audit,
            "current_user": current_user,
            "enqueue_node_task": enqueue_node_task,
            "public_task": public_task,
            "validate_platform_path": validate_platform_path,
            "select_storage_node_for_path": select_storage_node_for_path,
            "ensure_user_zfs_dataset_task": lambda conn, user_id, reason="ensure": ensure_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
                reason=reason,
            ),
        },
    )
    register_storage_routes(
        app,
        {
            "db": db,
            "mark_stale_nodes": mark_stale_nodes,
            "public_storage_image_file": public_storage_image_file,
            "current_user": current_user,
            "find_node_incus_image": find_node_incus_image,
            "storage_image_base_name": storage_image_base_name,
            "storage_root_for_node": storage_root_for_node,
            "now_ts": now_ts,
            "enqueue_node_task": enqueue_node_task,
            "audit": audit,
            "public_task": public_task,
            "node_has_incus_image": node_has_incus_image,
            "incus_image_import_payload": incus_image_import_payload,
            "ensure_user_zfs_dataset_task": lambda conn, user_id, reason="ensure": ensure_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
                reason=reason,
            ),
            "remove_user_zfs_dataset_task": lambda conn, user_id: remove_user_zfs_dataset_task(
                conn,
                user_id,
                now_ts,
                enqueue_node_task,
            ),
            "remove_user_workspace_volume_task": lambda conn, user_id, node_id: remove_user_workspace_volume_task(
                conn,
                user_id,
                node_id,
                now_ts,
                enqueue_node_task,
            ),
        },
    )
    register_container_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "audit": audit,
            "current_user": current_user,
            "usage_for_user": usage_for_user,
            "list_containers": list_containers,
            "list_container_ports": list_container_ports,
            "normalize_port_payload": normalize_port_payload,
            "add_container_port": add_container_port,
            "select_node_and_gpus": select_node_and_gpus,
            "build_data_mounts": build_data_mounts,
            "enqueue_incus_image_import_task": lambda conn, node, container_id, image_ref: enqueue_incus_image_import_task(
                conn,
                node,
                container_id,
                image_ref,
                now_ts,
                enqueue_node_task,
            ),
            "enqueue_node_task": enqueue_node_task,
            "public_task": public_task,
            "incus_create_payload": incus_create_payload,
            "incus_lifecycle_payload": incus_lifecycle_payload,
            "incus_ports_payload": incus_ports_payload,
            "select_storage_node_for_path": select_storage_node_for_path,
            "storage_root_for_node": storage_root_for_node,
            "storage_image_base_name": storage_image_base_name,
        },
    )
    register_metrics_routes(
        app,
        {
            "db": db,
            "mark_stale_nodes": mark_stale_nodes,
            "get_node": get_node,
            "gpu_with_container": gpu_with_container,
            "list_containers": list_containers,
        },
    )
    register_internal_routes(app, {"db": db, "now_ts": now_ts})
    register_agent_routes(
        app,
        {
            "db": db,
            "now_ts": now_ts,
            "audit": audit,
            "verify_agent_node": verify_agent_node,
            "upsert_node": upsert_node,
            "enqueue_node_task": enqueue_node_task,
        },
    )
    return app
