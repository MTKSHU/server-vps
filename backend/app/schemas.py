import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GPUReport(BaseModel):
    slot: int = 0
    uuid: str
    model: str
    pci_address: str = ""
    vram_gb: int = 0
    vram_used_mb: int = 0
    temperature_c: int = 35
    power_w: int = 40
    utilization: int = 0


class ContainerStateReport(BaseModel):
    name: str
    status: str = ""
    ip: str = ""
    role: str = ""


class IncusImageReport(BaseModel):
    fingerprint: str
    aliases: str = ""
    description: str = ""
    architecture: str = ""


class StorageVolumeReport(BaseModel):
    name: str
    path: str
    exists: bool = False
    total_gb: int = 0
    used_gb: int = 0
    free_gb: int = 0
    directory_used_gb: int = 0
    status: str = "unknown"
    error: str = ""


class ResourceReport(BaseModel):
    cpu_model: str = ""
    cpu_total: int = 1
    cpu_cores: int = 1
    cpu_sockets: int = 1
    cpu_temperature_c: int = 0
    memory_total_gb: int = 1
    disk_total_gb: float = 1
    cpu_used: int = 0
    memory_used_gb: int = 0
    disk_used_gb: float = 0
    load_avg: float = 0
    cpu_usage_percent: float = 0
    swap_total_gb: float = 0
    swap_used_gb: float = 0


class NodeRegistration(BaseModel):
    token: str
    hostname: str = ""
    ip: str = "unknown"
    node_group: str = "unassigned"
    driver_pool: str = "unknown"
    os_version: str = ""
    kernel_version: str = ""
    driver_version: str = ""
    cuda_driver_api_version: str = ""
    incus_status: str = "unknown"
    agent_version: str = ""
    uptime_seconds: int = 0
    resources: ResourceReport = Field(default_factory=ResourceReport)
    gpus: list[GPUReport] = Field(default_factory=list)
    containers: list[ContainerStateReport] | None = Field(default_factory=list)
    images: list[IncusImageReport] | None = Field(default_factory=list)
    storage_volumes: list[StorageVolumeReport] | None = Field(default_factory=list)


class JoinTokenCreate(BaseModel):
    expected_hostname: str = ""
    node_group: str = "unassigned"
    server_url: str = ""
    expires_in_hours: int = 24
    note: str = ""


class ContainerPortInput(BaseModel):
    name: str = ""
    protocol: str = "tcp"
    container_port: int


class ContainerResourceInput(BaseModel):
    resource_id: int
    mount_path: str = ""


class ContainerCreate(BaseModel):
    name: str
    image_id: str
    node_id: int | None = None
    cpu_cores: int
    memory_gb: int
    disk_gb: int
    gpu_count: int
    gpu_ids: list[int] = Field(default_factory=list)
    gpu_model: str = ""
    ssh_username: str = "ubuntu"
    ssh_key: str = ""
    resources: list[ContainerResourceInput] = Field(default_factory=list)
    mounts: list[str] = Field(default_factory=list)
    ports: list[ContainerPortInput] = Field(default_factory=list)
    expires_at: int = 0  # unix timestamp，0 = 永不到期


class NodeConfigInput(BaseModel):
    node_type: Literal["compute", "storage", "app", "mixed"] = "compute"
    schedulable: bool = True
    maintenance: bool = False
    max_containers: int = 8
    max_running_containers: int = 8
    max_gpu_shared_containers: int = 4
    allow_gpu_sharing: bool = True
    max_cpu_per_container: int = 0
    max_memory_gb_per_container: int = 0
    max_disk_gb_per_container: int = 0
    reserved_memory_gb: int = 0
    reserved_disk_gb: int = 0
    allow_port_mapping: bool = True
    max_ports_per_container: int = 8
    scheduler_weight: int = 0
    labels: list[str] = Field(default_factory=list)
    wol_mac: str = ""
    wol_broadcast: str = "255.255.255.255"
    ssh_user: str = "root"
    ssh_port: int = 22
    # 数据同步专用地址/端口；为空或 0 时回退到节点上报 ip / ssh_port
    sync_ip: str = ""
    sync_ssh_port: int = 0
    # 公开资源本地缓存根目录；为空时自动使用节点数据盘根目录下的 shared-cache 子目录
    resource_cache_base: str = ""

    @field_validator("sync_ip")
    @classmethod
    def _validate_sync_ip(cls, value: str) -> str:
        value = value.strip()
        if value == "":
            return value
        # 允许 IPv4 / IPv6 或主机名
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{1,253}", value):
            raise ValueError("sync_ip 必须是合法 IP 或主机名")
        return value

    @field_validator("sync_ssh_port")
    @classmethod
    def _validate_sync_ssh_port(cls, value: int) -> int:
        if value < 0 or value > 65535:
            raise ValueError("sync_ssh_port 必须在 0-65535 之间（0 表示使用默认 ssh_port）")
        return value


class ContainerDeleteRequest(BaseModel):
    name: str = ""
    force: bool = False


class ContainerResourceUpdate(BaseModel):
    cpu_cores: int
    memory_gb: int
    gpu_count: int
    gpu_model: str = ""


class ImageInput(BaseModel):
    id: str
    name: str
    incus_ref: str
    cuda_major: int = 0
    compatible_pools: str = "legacy-pascal,modern-geforce,workstation,unknown"
    owner: str = "admin"
    enabled: bool = True
    preferred: bool = False


class AgentTaskClaim(BaseModel):
    token: str
    hostname: str


class AgentTaskResult(BaseModel):
    token: str
    hostname: str
    ok: bool
    status: str = ""
    ip: str = ""
    output: str = ""
    error: str = ""


class AgentTaskProgress(BaseModel):
    token: str
    hostname: str
    progress: dict[str, Any] = {}


class ContainerExecCreate(BaseModel):
    command: str


class ContainerPublishImageInput(BaseModel):
    alias: str
    display_name: str = ""
    register_platform: bool = True
    export_to_storage: bool = True


class ContainerSyncInput(BaseModel):
    direction: Literal["storage_to_container", "container_to_storage"]
    storage_type: Literal["dataset", "model", "user_file"] = "user_file"
    resource_id: int | None = None
    storage_relative_path: str = ""
    container_path: str
    conflict_policy: Literal["overwrite", "skip"] = "overwrite"


class ContainerSyncRuleInput(BaseModel):
    name: str = ""
    container_path: str
    storage_relative_path: str = ""
    schedule_kind: Literal["daily", "weekly", "monthly"] = "daily"
    schedule_time_seconds: int = 0
    interval_minutes: int = 1440
    enabled: bool = True
    conflict_policy: Literal["overwrite", "skip"] = "overwrite"


class ContainerNodeCacheMountInput(BaseModel):
    resource_ids: list[int] = Field(default_factory=list)


class ContainerNodeCacheSyncInput(BaseModel):
    resource_ids: list[int] = Field(default_factory=list)


class UserPreferenceInput(BaseModel):
    value: dict[str, Any] = Field(default_factory=dict)


class UserProfileInput(BaseModel):
    display_name: str
    phone: str = ""


class SshKeyInput(BaseModel):
    label: str = ""
    public_key: str
    expires_at: int = 0  # 0 = 永久有效


class ApiTokenCreateInput(BaseModel):
    name: str = ""
    expires_at: int = 0  # 0 = 永久有效


class LoginInput(BaseModel):
    username: str
    password: str


class RegisterInput(BaseModel):
    username: str
    email: str
    password: str


class PasswordChangeInput(BaseModel):
    current_password: str = ""
    new_password: str


class UserUpsertInput(BaseModel):
    username: str
    display_name: str
    phone: str = ""
    email: str = ""
    group_name: Literal["platform_admin", "admin", "member", "guest"] = "member"
    password: str = ""
    ssh_key: str = ""
    enabled: bool = True
    cpu_cores: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    container_disk_limit_gb: int | None = None
    storage_quota_gb: int | None = None
    gpu_count: int | None = None
    container_count: int | None = None
    allowed_node_ids: list[int] = Field(default_factory=list)


class QuotaProfileInput(BaseModel):
    role: Literal["admin", "member"]
    cpu_cores: int
    memory_gb: int
    disk_gb: int
    container_disk_limit_gb: int
    storage_quota_gb: int
    gpu_count: int
    container_count: int
    allowed_node_ids: list[int] = Field(default_factory=list)


class UserDataPolicyInput(BaseModel):
    home_path: str = ""


class UserDirectoryScanInput(BaseModel):
    relative_path: str = ""
    limit: int = 500


class SharedResourceInput(BaseModel):
    resource_type: Literal["dataset", "huggingface_model", "pytorch_model"]
    name: str
    version: str = "default"
    source_path: str
    mount_path: str
    tags: list[str] = Field(default_factory=list)
    readonly: bool = True
    enabled: bool = True


class SharedResourceRequestInput(BaseModel):
    resource_type: Literal["dataset", "huggingface_model", "pytorch_model"]
    name: str
    version: str = "default"
    source: str = "huggingface"   # "huggingface" | "modelscope"
    download_mode: Literal["automatic", "manual"] = "automatic"
    tags: list[str] = Field(default_factory=list)
    # HuggingFace 参数
    hf_repo_id: str = ""
    hf_revision: str = "main"
    hf_token: str = ""
    hf_endpoint: str = ""
    # ModelScope 参数
    ms_repo_id: str = ""
    ms_revision: str = "master"
    ms_token: str = ""


class StorageSettingsInput(BaseModel):
    dataset_base_path: str = "/data/datasets"
    model_base_path: str = "/data/models"
    user_base_path: str = "/data/users"
    hf_endpoint: str = ""
    hf_endpoint_enabled: bool = False
    hf_download_engine: Literal["auto", "sdk", "hfd"] = "auto"


class PlatformSettingsInput(BaseModel):
    local_login_enabled: bool = True
    platform_registration_enabled: bool = False
    platform_registration_auto_enable: bool = False
    platform_registration_default_group: Literal["platform_admin", "admin", "member", "guest"] = "member"
    sso_registration_enabled: bool = True
    sso_auto_create_users: bool = True
    sso_auto_enable_new_users: bool = False
    sso_default_group: Literal["platform_admin", "admin", "member", "guest"] = "member"
    platform_timezone: str = "Asia/Shanghai"
    transfer_bandwidth_limit_mbps: int = 0
    agent_metrics_interval_seconds: int = 2
    agent_heartbeat_interval_seconds: int = 15
    agent_container_interval_seconds: int = 15
    agent_storage_interval_seconds: int = 60
    agent_inventory_interval_seconds: int = 300
    agent_task_poll_interval_seconds: int = 5
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_secret: str = ""
    sso_provider_enabled: bool = False
    sso_provider_type: Literal["oidc", "cas"] = "oidc"
    sso_provider_name: str = "casdoor"
    sso_provider_display_name: str = "统一认证"
    sso_callback_base_url: str = ""
    sso_cas_server_url: str = ""
    sso_cas_version: int = 3
    sso_oidc_issuer: str = ""
    sso_oidc_authorization_endpoint: str = ""
    sso_oidc_token_endpoint: str = ""
    sso_oidc_userinfo_endpoint: str = ""
    sso_oidc_client_id: str = ""
    sso_oidc_client_secret: str = ""
    sso_oidc_scopes: str = "openid profile email"
    sso_casdoor_admin_owner: str = "built-in"


class AgentMetricsInput(BaseModel):
    token: str
    hostname: str
    uptime_seconds: int = 0
    cpu_usage_percent: float = 0
    cpu_temperature_c: int = 0
    memory_total_gb: int = 1
    memory_used_gb: int = 0
    load_avg: float = 0
    swap_total_gb: float = 0
    swap_used_gb: float = 0
    gpus: list[GPUReport] = Field(default_factory=list)


class StorageImageExportInput(BaseModel):
    source_node_id: int
    image_ref: str
    alias: str = ""


class StorageImageDistributeInput(BaseModel):
    target_node_ids: list[int] = Field(default_factory=list)


class SharedResourceTagsInput(BaseModel):
    tags: list[str] = Field(default_factory=list)


class SharedResourceInfoInput(BaseModel):
    name: str
    version: str = "default"
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 响应 Model（用于 FastAPI response_model，驱动 OpenAPI 类型生成）
# ---------------------------------------------------------------------------

class AlertItem(BaseModel):
    """集群告警条目。"""
    level: Literal["error", "warning", "info"]
    type: str
    message: str
    node_id: int | None = None


class SummaryResponse(BaseModel):
    """GET /api/summary 响应体。"""
    nodes_online: int
    nodes_total: int
    gpus_free: int
    gpus_total: int
    containers_running: int
    containers_total: int
    cpu_used: int
    cpu_total: int
    memory_used_gb: float
    memory_total_gb: float
    disk_used_gb: float
    disk_total_gb: float
    alerts: list[AlertItem] = Field(default_factory=list)


class NodeTaskOut(BaseModel):
    """节点任务摘要（供前端展示）。"""
    id: int
    node_id: int
    container_id: int | None = None
    data_sync_task_id: int | None = None
    type: str
    status: str
    attempts: int = 0
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: int
    claimed_at: int | None = None
    finished_at: int | None = None
    available_at: int = 0
    updated_at: int


class SyncProgressOut(BaseModel):
    phase: str | None = None
    pct: int | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    rate: str | None = None
    current_file: str | None = None


class DataSyncTaskOut(BaseModel):
    """GET /api/containers/{id}/sync-tasks 响应元素。"""
    id: int
    task_type: str
    user_id: int | None = None
    resource_id: int | None = None
    source_node_id: int | None = None
    target_node_id: int | None = None
    container_id: int | None = None
    source_path: str = ""
    target_path: str = ""
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)
    progress: SyncProgressOut | None = None
    created_at: int
    updated_at: int
    finished_at: int = 0
    last_error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class ImageOut(BaseModel):
    """GET /api/images 响应元素。"""
    id: str
    name: str
    cuda_major: int = 0
    compatible_pools: str = ""
    incus_ref: str = ""
    enabled: bool = True
    preferred: bool = False
    owner: str = "admin"
    created_at: int
    updated_at: int


class IncusImageOut(BaseModel):
    """节点本地 Incus 镜像条目。"""
    node_id: int
    node: str
    node_status: str = ""
    fingerprint: str = ""
    aliases: str = ""
    description: str = ""
    architecture: str = ""
    updated_at: int = 0


class ImageCatalogOut(BaseModel):
    """GET /api/image-catalog 响应体。"""
    images: list[ImageOut] = Field(default_factory=list)
    incus_images: list[IncusImageOut] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """GET /api/health 响应体。"""
    ok: bool
    database: str


# ---------------------------------------------------------------------------
# 核心业务实体响应 Model
# 字段与前端 cluster.ts 中对应 interface 保持一致，方便后续 openapi-typescript 替换。
# ---------------------------------------------------------------------------

class GpuOut(BaseModel):
    id: int
    slot: int
    uuid: str
    model: str
    pci_address: str = ""
    vram_gb: int


class ContainerPortOut(BaseModel):
    id: int
    container_id: int = 0       # 部分调用路径不携带此字段，给默认值避免验证失败
    name: str
    protocol: str
    container_port: int
    host_port: int
    node_port: int | None = None
    public_port: int | None = None
    node_listen_port: int | None = None
    created_at: int = 0         # 同上
    updated_at: int = 0         # 同上


class ContainerOut(BaseModel):
    """GET /api/containers 响应元素。"""
    id: int
    name: str
    owner_id: int
    node_id: int
    image_id: str
    image_name: str = ""
    owner: str = ""
    node: str = ""
    node_ip: str = ""
    status: str
    access_status: str = "pending"
    access_error: str = ""
    system_role: str = ""
    cpu_cores: int
    memory_gb: int
    disk_gb: int
    ssh_username: str = "ubuntu"
    ip: str = ""
    mounts: list[str] = Field(default_factory=list)
    created_at: int
    updated_at: int
    gpus: list[GpuOut] = Field(default_factory=list)
    ports: list[ContainerPortOut] = Field(default_factory=list)


class NodeOut(BaseModel):
    """GET /api/nodes 响应元素。"""
    id: int
    hostname: str
    ip: str
    node_type: str = "compute"
    driver_pool: str = "unknown"
    status: str
    schedulable: bool = True
    maintenance: bool = False
    max_containers: int = 8
    max_running_containers: int = 8
    max_gpu_shared_containers: int = 4
    allow_gpu_sharing: bool = True
    max_cpu_per_container: int = 0
    max_memory_gb_per_container: int = 0
    max_disk_gb_per_container: int = 0
    reserved_memory_gb: int = 0
    reserved_disk_gb: int = 0
    allow_port_mapping: bool = True
    max_ports_per_container: int = 8
    scheduler_weight: int = 0
    labels: list[str] = Field(default_factory=list)
    wol_mac: str = ""
    wol_broadcast: str = "255.255.255.255"
    ssh_user: str = "root"
    ssh_port: int = 22
    sync_ip: str = ""
    sync_ssh_port: int = 0
    resource_cache_base: str = ""
    cpu_model: str = ""
    cpu_total: int = 0
    cpu_cores: int = 0
    cpu_sockets: int = 1
    cpu_temperature_c: int = 0
    memory_total_gb: int = 0
    disk_total_gb: float = 0
    cpu_used: int = 0
    memory_used_gb: int = 0
    disk_used_gb: float = 0
    last_seen: int = 0
    load_avg: float = 0
    cpu_usage_percent: float = 0
    swap_total_gb: float = 0
    swap_used_gb: float = 0
    os_version: str = ""
    kernel_version: str = ""
    driver_version: str = ""
    cuda_driver_api_version: str = ""
    incus_status: str = "unknown"
    agent_version: str = ""
    uptime_seconds: int = 0
    agent_update_channel: str = "stable"
    agent_auto_update: bool = False
    target_agent_version: str = ""
    agent_update_status: str = ""
    agent_update_error: str = ""
    agent_update_at: int = 0
    registered_at: int = 0
    gpus: list[GpuOut] = Field(default_factory=list)


class UserOut(BaseModel):
    """GET /api/users 响应元素（不含密码 hash）。"""
    # 允许后端传递额外字段（如 SSO 合并逻辑产生的扩展字段），不会因 model 不完整而报错
    model_config = ConfigDict(extra="ignore")
    id: int
    username: str
    display_name: str = ""
    role: str = "member"
    phone: str = ""
    email: str = ""
    group_name: str = "member"
    enabled: bool = True
    ssh_key: str = ""
    allowed_node_ids: list[int] = Field(default_factory=list)
    # 额度字段（可能为 null）
    cpu_cores: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    container_disk_limit_gb: int | None = None
    storage_quota_gb: int | None = None
    gpu_count: int | None = None
    container_count: int | None = None
    # SSO 相关（可选）
    pending_sso: bool | None = None
    casdoor_id: str | None = None
