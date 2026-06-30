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
}

type ContainerReport struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	IP     string `json:"ip"`
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
	ContainerID int         `json:"container_id"`
	Name        string      `json:"name"`
	Image       string      `json:"image"`
	CPUCores    int         `json:"cpu_cores"`
	MemoryGB    int         `json:"memory_gb"`
	DiskGB      int         `json:"disk_gb"`
	SSHUsername string      `json:"ssh_username"`
	SSHKey      string      `json:"ssh_key"`
	Mounts      []string    `json:"mounts"`
	GPUs        []IncusGPU  `json:"gpus"`
	Ports       []IncusPort `json:"ports"`
	// workspace: named Incus storage volume mounted at /workspace
	WorkspaceVolumeName string `json:"workspace_volume_name"`
	WorkspaceVolumeGB   int    `json:"workspace_volume_gb"`
}

type IncusExecPayload struct {
	ContainerID int    `json:"container_id"`
	Name        string `json:"name"`
	Command     string `json:"command"`
}

type IncusConfigUpdatePayload struct {
	ContainerID int        `json:"container_id"`
	Name        string     `json:"name"`
	CPUCores    int        `json:"cpu_cores"`
	MemoryGB    int        `json:"memory_gb"`
	GPUs        []IncusGPU `json:"gpus"`
}

type IncusSSHKeysPayload struct {
	ContainerID int      `json:"container_id"`
	Name        string   `json:"name"`
	SSHUsername string   `json:"ssh_username"`
	SSHKey      string   `json:"ssh_key"`
	Mounts      []string `json:"mounts"`
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
	ContainerID int         `json:"container_id"`
	Name        string      `json:"name"`
	SSHUsername string      `json:"ssh_username"`
	SSHKey      string      `json:"ssh_key"`
	Mounts      []string    `json:"mounts"`
	Ports       []IncusPort `json:"ports"`
}

type IncusLifecyclePayload struct {
	ContainerID    int    `json:"container_id"`
	Name           string `json:"name"`
	Operation      string `json:"operation"`
	PreviousStatus string `json:"previous_status"`
}

type IncusImagePullPayload struct {
	ImageRef string `json:"image_ref"`
	Alias    string `json:"alias"`
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
	ResourceID   int    `json:"resource_id"`
	ResourceType string `json:"resource_type"`
	Name         string `json:"name"`
	Version      string `json:"version"`
	SourcePath   string `json:"source_path"`
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
	UserID           int    `json:"user_id"`
	Username         string `json:"username"`
	PlatformHomePath string `json:"platform_home_path"`
	Mountpoint       string `json:"mountpoint"`
	DatasetName      string `json:"dataset_name"`
	QuotaGB          int    `json:"quota_gb"`
	UID              int    `json:"uid"`
	GID              int    `json:"gid"`
	Mode             string `json:"mode"`
	Reason           string `json:"reason"`
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
