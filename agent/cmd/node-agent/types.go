package main

import (
	"encoding/json"
)

var agentVersion = "dev"

type GPUReport struct {
	Slot         int    `json:"slot"`
	UUID         string `json:"uuid"`
	Model        string `json:"model"`
	PCIAddress   string `json:"pci_address"`
	VRAMGB       int    `json:"vram_gb"`
	VRAMUsedMB   int    `json:"vram_used_mb"`
	TemperatureC int    `json:"temperature_c"`
	PowerW       int    `json:"power_w"`
	Utilization  int    `json:"utilization"`
}

type ResourceReport struct {
	CPUModel        string  `json:"cpu_model"`
	CPUTotal        int     `json:"cpu_total"`
	CPUCores        int     `json:"cpu_cores"`
	CPUSockets      int     `json:"cpu_sockets"`
	CPUTemperatureC int     `json:"cpu_temperature_c"`
	MemoryTotalGB   int     `json:"memory_total_gb"`
	DiskTotalGB     float64 `json:"disk_total_gb"`
	CPUUsed         int     `json:"cpu_used"`
	MemoryUsedGB    int     `json:"memory_used_gb"`
	DiskUsedGB      float64 `json:"disk_used_gb"`
	LoadAvg         float64 `json:"load_avg"`
	CPUUsagePercent float64 `json:"cpu_usage_percent"`
	SwapTotalGB     float64 `json:"swap_total_gb"`
	SwapUsedGB      float64 `json:"swap_used_gb"`
}

type NodeRegistration struct {
	Token                string             `json:"token"`
	Hostname             string             `json:"hostname"`
	IP                   string             `json:"ip"`
	NodeGroup            string             `json:"node_group"`
	DriverPool           string             `json:"driver_pool"`
	OSVersion            string             `json:"os_version"`
	KernelVersion        string             `json:"kernel_version"`
	DriverVersion        string             `json:"driver_version"`
	CUDADriverAPIVersion string             `json:"cuda_driver_api_version"`
	IncusStatus          string             `json:"incus_status"`
	AgentVersion         string             `json:"agent_version"`
	UptimeSeconds        int64              `json:"uptime_seconds"`
	Resources            ResourceReport     `json:"resources"`
	GPUs                 []GPUReport        `json:"gpus"`
	Containers           []ContainerReport  `json:"containers"`
	Images               []IncusImageReport `json:"images"`
	StorageVolumes       []StorageVolume    `json:"storage_volumes"`
	Capabilities         []string           `json:"capabilities"`
	NFSHealth            NFSHealthReport    `json:"nfs_health"`
}

type NFSHealthReport struct {
	Healthy   bool    `json:"healthy"`
	LatencyMS float64 `json:"latency_ms"`
	Error     string  `json:"error"`
	CheckedAt int64   `json:"checked_at"`
}

type AgentCollectionConfig struct {
	MetricsIntervalSeconds   int `json:"metrics_interval_seconds"`
	HeartbeatIntervalSeconds int `json:"heartbeat_interval_seconds"`
	ContainerIntervalSeconds int `json:"container_interval_seconds"`
	StorageIntervalSeconds   int `json:"storage_interval_seconds"`
	InventoryIntervalSeconds int `json:"inventory_interval_seconds"`
	TaskPollIntervalSeconds  int `json:"task_poll_interval_seconds"`
}

type AgentRegistrationResponse struct {
	AgentConfig AgentCollectionConfig `json:"agent_config"`
}

type AgentMetricsReport struct {
	Token                   string      `json:"token"`
	Hostname                string      `json:"hostname"`
	UptimeSeconds           int64       `json:"uptime_seconds"`
	CPUUsagePercent         float64     `json:"cpu_usage_percent"`
	CPUTemperatureC         int         `json:"cpu_temperature_c"`
	MemoryTotalGB           int         `json:"memory_total_gb"`
	MemoryUsedGB            int         `json:"memory_used_gb"`
	LoadAvg                 float64     `json:"load_avg"`
	SwapTotalGB             float64     `json:"swap_total_gb"`
	SwapUsedGB              float64     `json:"swap_used_gb"`
	NetworkInterface        string      `json:"network_interface"`
	NetworkRXBytesPerSecond float64     `json:"network_rx_bytes_per_sec"`
	NetworkTXBytesPerSecond float64     `json:"network_tx_bytes_per_sec"`
	GPUs                    []GPUReport `json:"gpus"`
}

type ContainerReport struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	IP     string `json:"ip"`
	Role   string `json:"role"`
}

type IncusImageReport struct {
	Fingerprint  string `json:"fingerprint"`
	Aliases      string `json:"aliases"`
	Description  string `json:"description"`
	Architecture string `json:"architecture"`
}

type StorageVolume struct {
	Name            string `json:"name"`
	Path            string `json:"path"`
	Exists          bool   `json:"exists"`
	TotalGB         int    `json:"total_gb"`
	UsedGB          int    `json:"used_gb"`
	FreeGB          int    `json:"free_gb"`
	DirectoryUsedGB int    `json:"directory_used_gb"`
	Status          string `json:"status"`
	Error           string `json:"error"`
}

type TaskClaimRequest struct {
	Token    string `json:"token"`
	Hostname string `json:"hostname"`
}

type TaskResultRequest struct {
	Token    string `json:"token"`
	Hostname string `json:"hostname"`
	OK       bool   `json:"ok"`
	Status   string `json:"status"`
	IP       string `json:"ip"`
	Output   string `json:"output"`
	Error    string `json:"error"`
}

// SyncProgress 描述 rsync 数据同步的实时进度，结构与后端下载进度保持一致，
// 便于前端复用展示逻辑。
type SyncProgress struct {
	Phase       string `json:"phase"`
	Pct         int    `json:"pct"`
	BytesDone   int64  `json:"bytes_done"`
	BytesTotal  int64  `json:"bytes_total"`
	Rate        string `json:"rate"`
	CurrentFile string `json:"current_file"`
}

type TaskProgressRequest struct {
	Token    string       `json:"token"`
	Hostname string       `json:"hostname"`
	Progress SyncProgress `json:"progress"`
}

type TaskEnvelope struct {
	Task *AgentTask `json:"task"`
}

type AgentTask struct {
	ID       int             `json:"id"`
	Type     string          `json:"type"`
	Payload  json.RawMessage `json:"payload"`
	Attempts int             `json:"attempts"`
}

type IncusGPU struct {
	Slot       int    `json:"slot"`
	UUID       string `json:"uuid"`
	Model      string `json:"model"`
	PCIAddress string `json:"pci_address"`
}

type IncusPort struct {
	ID            int    `json:"id"`
	Name          string `json:"name"`
	Protocol      string `json:"protocol"`
	ContainerPort int    `json:"container_port"`
	HostPort      int    `json:"host_port"`
	NodePort      int    `json:"node_port"`
}

type IncusCreatePayload struct {
	ContainerID   int                 `json:"container_id"`
	Name          string              `json:"name"`
	Image         string              `json:"image"`
	CPUCores      int                 `json:"cpu_cores"`
	MemoryGB      int                 `json:"memory_gb"`
	DiskGB        int                 `json:"disk_gb"`
	SSHUsername   string              `json:"ssh_username"`
	SSHKey        string              `json:"ssh_key"`
	Mounts        []string            `json:"mounts"`
	ManagedMounts []ManagedMount      `json:"managed_mounts"`
	SharedStorage SharedStorageConfig `json:"shared_storage"`
	GPUs          []IncusGPU          `json:"gpus"`
	Ports         []IncusPort         `json:"ports"`
	// workspace: named Incus storage volume mounted at /workspace
	WorkspaceVolumeName string `json:"workspace_volume_name"`
	WorkspaceVolumeGB   int    `json:"workspace_volume_gb"`
}

type ManagedMount struct {
	Kind     string `json:"kind"`
	Source   string `json:"source"`
	Target   string `json:"target"`
	Readonly bool   `json:"readonly"`
	Required bool   `json:"required"`
	// Export is set for per-user NFS exports.  The agent mounts this export
	// directly on Source instead of assuming Source is below a shared parent
	// users export.  Empty keeps the v1 parent-export behaviour.
	Export string `json:"export,omitempty"`
}

type SharedStorageConfig struct {
	Enabled           bool   `json:"enabled"`
	Server            string `json:"server"`
	UsersExport       string `json:"users_export"`
	DatasetsExport    string `json:"datasets_export"`
	ModelsExport      string `json:"models_export"`
	MountOptions      string `json:"mount_options"`
	Sentinel          string `json:"sentinel"`
	SentinelSignature string `json:"sentinel_signature"`
	IDMapBase         int    `json:"idmap_base"`
}

type IncusExecPayload struct {
	ContainerID int    `json:"container_id"`
	Name        string `json:"name"`
	Command     string `json:"command"`
}

type ContainerHomeMigrationPayload struct {
	ContainerID   int                 `json:"container_id"`
	Name          string              `json:"name"`
	SSHUsername   string              `json:"ssh_username"`
	Owner         string              `json:"owner"`
	Primary       bool                `json:"primary"`
	ManagedMount  ManagedMount        `json:"managed_mount"`
	SharedStorage SharedStorageConfig `json:"shared_storage"`
}

type IncusConfigUpdatePayload struct {
	ContainerID int        `json:"container_id"`
	Name        string     `json:"name"`
	CPUCores    int        `json:"cpu_cores"`
	MemoryGB    int        `json:"memory_gb"`
	GPUs        []IncusGPU `json:"gpus"`
}

type IncusSSHKeysPayload struct {
	ContainerID   int            `json:"container_id"`
	Name          string         `json:"name"`
	SSHUsername   string         `json:"ssh_username"`
	SSHKey        string         `json:"ssh_key"`
	Mounts        []string       `json:"mounts"`
	ManagedMounts []ManagedMount `json:"managed_mounts"`
}

type IncusPublishPayload struct {
	ContainerID        int    `json:"container_id"`
	Name               string `json:"name"`
	Alias              string `json:"alias"`
	StorageImageFileID int    `json:"storage_image_file_id"`
	ExportDir          string `json:"export_dir"`
	BaseName           string `json:"base_name"`
}

type IncusPortsPayload struct {
	ContainerID   int            `json:"container_id"`
	Name          string         `json:"name"`
	SSHUsername   string         `json:"ssh_username"`
	SSHKey        string         `json:"ssh_key"`
	Mounts        []string       `json:"mounts"`
	ManagedMounts []ManagedMount `json:"managed_mounts"`
	Ports         []IncusPort    `json:"ports"`
}

type IncusLifecyclePayload struct {
	ContainerID    int                 `json:"container_id"`
	Name           string              `json:"name"`
	Operation      string              `json:"operation"`
	PreviousStatus string              `json:"previous_status"`
	ManagedMounts  []ManagedMount      `json:"managed_mounts"`
	SharedStorage  SharedStorageConfig `json:"shared_storage"`
}

type IncusImagePullPayload struct {
	ImageRef string `json:"image_ref"`
	Alias    string `json:"alias"`
}

type IncusDeleteImagePayload struct {
	ImageRef string `json:"image_ref"` // alias 或 fingerprint
}

type SshPubkeyInstallPayload struct {
	Pubkey string `json:"pubkey"`
}

type IncusImageExportPayload struct {
	StorageImageFileID int    `json:"storage_image_file_id"`
	ImageRef           string `json:"image_ref"`
	Alias              string `json:"alias"`
	ExportDir          string `json:"export_dir"`
	BaseName           string `json:"base_name"`
}

type IncusImageCleanupPayload struct {
	StorageImageFileID int    `json:"storage_image_file_id"`
	ExportDir          string `json:"export_dir"`
	BaseName           string `json:"base_name"`
	Fingerprint        string `json:"fingerprint"`
}

type IncusImageImportPayload struct {
	StorageImageFileID int                 `json:"storage_image_file_id"`
	SourceNodeID       int                 `json:"source_node_id"`
	TargetNodeID       int                 `json:"target_node_id"`
	SourcePath         string              `json:"source_path"`
	TargetPath         string              `json:"target_path"`
	BaseName           string              `json:"base_name"`
	Alias              string              `json:"alias"`
	Fingerprint        string              `json:"fingerprint"`
	SourceEndpoint     DataSyncSSHEndpoint `json:"source_endpoint"`
}

type SharedResourceVerifyPayload struct {
	ResourceID           int    `json:"resource_id"`
	ResourceType         string `json:"resource_type"`
	Name                 string `json:"name"`
	Version              string `json:"version"`
	SourcePath           string `json:"source_path"`
	Source               string `json:"source"`
	RepoID               string `json:"repo_id"`
	Revision             string `json:"revision"`
	Token                string `json:"token"`
	RepoType             string `json:"repo_type"`
	HFEndpoint           string `json:"hf_endpoint"`
	AllowOfflineManifest bool   `json:"allow_offline_manifest"`
	ManualFinalize       bool   `json:"manual_finalize"`
}

type SharedResourceScanPayload struct {
	ResourceID   int    `json:"resource_id"`
	RelativePath string `json:"relative_path"`
	RootPath     string `json:"root_path"`
	Path         string `json:"path"`
	Limit        int    `json:"limit"`
}

type UserDirectoryScanPayload struct {
	UserID       int    `json:"user_id"`
	Username     string `json:"username"`
	RelativePath string `json:"relative_path"`
	RootPath     string `json:"root_path"`
	Path         string `json:"path"`
	Limit        int    `json:"limit"`
}

type UserZFSDatasetPayload struct {
	UserID               int    `json:"user_id"`
	Username             string `json:"username"`
	PlatformHomePath     string `json:"platform_home_path"`
	Mountpoint           string `json:"mountpoint"`
	DatasetName          string `json:"dataset_name"`
	QuotaGB              int    `json:"quota_gb"`
	UID                  int    `json:"uid"`
	GID                  int    `json:"gid"`
	Mode                 string `json:"mode"`
	Reason               string `json:"reason"`
	Sentinel             string `json:"sentinel"`
	SentinelSignature    string `json:"sentinel_signature"`
	EnsureNFSShare       bool   `json:"ensure_nfs_share"`
	NFSShareTemplatePath string `json:"nfs_share_template_path"`
}

type UserZFSDatasetRemovePayload struct {
	UserID      int    `json:"user_id"`
	DatasetName string `json:"dataset_name"`
	Mountpoint  string `json:"mountpoint"`
}

type UserWorkspaceVolumeRemovePayload struct {
	UserID     int    `json:"user_id"`
	NodeID     int    `json:"node_id"`
	VolumeName string `json:"volume_name"`
}

type DataSyncSSHEndpoint struct {
	Hostname     string `json:"hostname"`
	Host         string `json:"host"`
	Port         int    `json:"port"`
	User         string `json:"user"`
	IdentityFile string `json:"identity_file"`
	// PrivateKey 优先于 IdentityFile；提供时会把内容写入临时密钥文件使用。
	PrivateKey string `json:"private_key"`
	// Restricted 为 true 时表示远端 authorized_keys 使用 command= 限制，不能执行 mkdir 等命令。
	Restricted bool `json:"restricted"`
	// AllowedPath 是 Restricted 模式下 rrsync 允许的根目录；客户端需把绝对路径转换为相对路径。
	AllowedPath string `json:"allowed_path"`
	// JumpHost 为空时直连，非空时通过该跳板机中转（格式：user@host 或 user@host:port）
	JumpHost string `json:"jump_host"`
}

type DataSyncPayload struct {
	SyncTaskID     int                 `json:"sync_task_id"`
	SourceNodeID   int                 `json:"source_node_id"`
	TargetNodeID   int                 `json:"target_node_id"`
	ContainerName  string              `json:"container_name"`
	SourcePath     string              `json:"source_path"`
	TargetPath     string              `json:"target_path"`
	SourceRoot     string              `json:"source_root"`
	TargetRoot     string              `json:"target_root"`
	Username       string              `json:"username"`
	Mode           string              `json:"mode"`
	Delete         bool                `json:"delete"`
	IgnoreExisting bool                `json:"ignore_existing"`
	BandwidthLimit int                 `json:"bandwidth_limit_mbps"`
	SourceEndpoint DataSyncSSHEndpoint `json:"source_endpoint"`
	TargetEndpoint DataSyncSSHEndpoint `json:"target_endpoint"`
	Update         bool                `json:"update"`
}

type InstallSyncPubkeyPayload struct {
	PublicKey   string `json:"public_key"`
	AllowedPath string `json:"allowed_path"`
	KeyID       string `json:"key_id"`
	ExpiresAt   int64  `json:"expires_at"`
}

type RemoveSyncPubkeyPayload struct {
	KeyID     string `json:"key_id"`
	PublicKey string `json:"public_key"`
}

type SyncSharedResourcePayload struct {
	ResourceID       int    `json:"resource_id"`
	SourceHost       string `json:"source_host"`
	SourcePort       int    `json:"source_port"`
	SourceUser       string `json:"source_user"`
	SourcePath       string `json:"source_path"`
	SourcePrivateKey string `json:"source_private_key"`
	LocalCachePath   string `json:"local_cache_path"`
}

type DownloadSharedResourcePayload struct {
	ResourceID       int    `json:"resource_id"`
	ResourceType     string `json:"resource_type"`
	Name             string `json:"name"`
	Version          string `json:"version"`
	Source           string `json:"source"`
	RepoID           string `json:"repo_id"`
	Revision         string `json:"revision"`
	Token            string `json:"token"`
	RepoType         string `json:"repo_type"`
	TargetPath       string `json:"target_path"`
	StagingPath      string `json:"staging_path"`
	HFEndpoint       string `json:"hf_endpoint"`
	HFDownloadEngine string `json:"hf_download_engine"`
	FallbackRepoID   string `json:"fallback_repo_id"`
	FallbackRevision string `json:"fallback_revision"`
}

type MigrateSharedResourcePathPayload struct {
	ResourceID    int    `json:"resource_id"`
	ResourceType  string `json:"resource_type"`
	Name          string `json:"name"`
	Version       string `json:"version"`
	OldPath       string `json:"old_path"`
	NewPath       string `json:"new_path"`
	OldSourcePath string `json:"old_source_path"`
	NewSourcePath string `json:"new_source_path"`
	CreateSymlink bool   `json:"create_symlink"`
}

// MountUpdate 描述一条资源挂载的更新指令
type MountUpdate struct {
	// OldTarget 是容器内旧挂载点（可能与 NewTarget 不同）
	OldTarget string `json:"old_target"`
	// NewSource 是宏机上新的本地路径
	NewSource string `json:"new_source"`
	// NewTarget 是容器内新挂载点
	NewTarget string `json:"new_target"`
	Readonly  bool   `json:"readonly"`
}

type ApplyResourceMountsPayload struct {
	ContainerID   int                 `json:"container_id"`
	Name          string              `json:"name"`
	MountUpdates  []MountUpdate       `json:"mount_updates"`
	ManagedMounts []ManagedMount      `json:"managed_mounts,omitempty"`
	SharedStorage SharedStorageConfig `json:"shared_storage,omitempty"`
}

type RemoveResourceMountsPayload struct {
	ContainerID int      `json:"container_id"`
	Name        string   `json:"name"`
	Targets     []string `json:"targets"`
}
