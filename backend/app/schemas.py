import ipaddress
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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


class UserPreferenceInput(BaseModel):
    value: dict[str, Any] = Field(default_factory=dict)


class UserProfileInput(BaseModel):
    display_name: str
    phone: str = ""


class SshKeyInput(BaseModel):
    label: str = ""
    public_key: str
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
    tags: list[str] = Field(default_factory=list)
    # HuggingFace 参数
    hf_repo_id: str = ""
    hf_revision: str = "main"
    hf_token: str = ""
    # ModelScope 参数
    ms_repo_id: str = ""
    ms_revision: str = "master"
    ms_token: str = ""


class StorageSettingsInput(BaseModel):
    dataset_base_path: str = "/data/datasets"
    model_base_path: str = "/data/models/huggingface"
    user_base_path: str = "/data/users"
    hf_endpoint: str = ""
    hf_endpoint_enabled: bool = False


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
