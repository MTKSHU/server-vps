import { authToken } from "../auth";
import { patchJson, postJson, request } from "./client";
import { translateApiError } from "./errors";
import type { components } from "./schema";

// ── Auto-generated request body types (from openapi.json via openapi-typescript)
// Run `npm run gen-api` to regenerate after backend changes.
// NOTE: Response types (Container, Node, User, …) are NOT auto-generated because
// the backend returns plain dicts without Pydantic response_model annotations.
type NodeConfigInput     = components["schemas"]["NodeConfigInput"];
// Fields with server-side defaults (@default in OpenAPI) are made optional so
// callers don't have to supply them when the default is acceptable.
export type ContainerCreateInput = Omit<
  components["schemas"]["ContainerCreate"],
  "gpu_model" | "ssh_username" | "ssh_key"
> & {
  gpu_model?: string;
  ssh_username?: string;
  ssh_key?: string;
};
export type ContainerSyncRuleInput = components["schemas"]["ContainerSyncRuleInput"];

// ── SSO 统一认证 ──────────────────────────────────────────────────────────────
export interface SSOProvider {
  id: string;
  display_name: string;
}

export interface AuthConfig {
  local_login_enabled: boolean;
  sso_login_enabled: boolean;
  registration_enabled: boolean;
  registration_mode: "platform" | "sso" | "disabled";
  default_register_group: string;
  platform_registration_auto_enable: boolean;
}

export interface PlatformSettings {
  local_login_enabled: boolean;
  platform_registration_enabled: boolean;
  platform_registration_auto_enable: boolean;
  platform_registration_default_group: "platform_admin" | "admin" | "member" | "guest";
  sso_registration_enabled: boolean;
  sso_auto_create_users: boolean;
  sso_auto_enable_new_users: boolean;
  sso_default_group: "platform_admin" | "admin" | "member" | "guest";
  platform_timezone: string;
  transfer_bandwidth_limit_mbps: number;
  webhook_enabled: boolean;
  webhook_url: string;
  webhook_secret: string;
  sso_provider_enabled: boolean;
  sso_provider_type: "oidc" | "cas";
  sso_provider_name: string;
  sso_provider_display_name: string;
  sso_callback_base_url: string;
  sso_cas_server_url: string;
  sso_cas_version: number;
  sso_oidc_issuer: string;
  sso_oidc_authorization_endpoint: string;
  sso_oidc_token_endpoint: string;
  sso_oidc_userinfo_endpoint: string;
  sso_oidc_client_id: string;
  sso_oidc_client_secret: string;
  sso_oidc_scopes: string;
  sso_casdoor_admin_owner: string;
}

export interface RegisterResult {
  ok: boolean;
  enabled: boolean;
  auto_enable: boolean;
  user: import("../auth").AuthUser;
}

export function getAuthConfig(): Promise<AuthConfig> {
  return request<AuthConfig>("/api/auth/config");
}

export function getPlatformSettings() {
  return request<PlatformSettings>("/api/platform/settings");
}

export function updatePlatformSettings(payload: PlatformSettings) {
  return request<PlatformSettings>("/api/platform/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getSSOProviders(): Promise<SSOProvider[]> {
  return request<SSOProvider[]>("/api/auth/sso/providers");
}

export function ssoCallback(params: { state: string; code?: string; ticket?: string }) {
  return postJson<{ token: string; expires_at: number; user: unknown }>("/api/auth/sso/callback", params);
}

export function ssoStartUrl(providerId: string): string {
  return `/api/auth/sso/start/${encodeURIComponent(providerId)}`;
}

export interface Summary {
  nodes_online: number;
  nodes_total: number;
  gpus_free: number;
  gpus_total: number;
  containers_running: number;
  containers_total: number;
  cpu_used: number;
  cpu_total: number;
  memory_used_gb: number;
  memory_total_gb: number;
  disk_used_gb: number;
  disk_total_gb: number;
  alerts: Array<{
    level: "error" | "warning" | "info";
    type: string;
    message: string;
    node_id?: number | null;
  }>;
}

export interface Gpu {
  id: number;
  node_id: number;
  hostname?: string;
  slot: number;
  uuid: string;
  model: string;
  pci_address?: string;
  vram_gb: number;
  vram_used_mb: number;
  temperature_c: number;
  power_w: number;
  utilization: number;
  container?: { id: number; name: string; status: string; owner: string } | null;
  containers?: Array<{ id: number; name: string; status: string; owner: string }>;
}

export interface Node {
  id: number;
  hostname: string;
  ip: string;
  node_type: "compute" | "storage" | "app" | "mixed";
  driver_pool: string;
  status: string;
  schedulable: boolean;
  maintenance: boolean;
  max_containers: number;
  max_running_containers: number;
  max_gpu_shared_containers: number;
  allow_gpu_sharing: boolean;
  max_cpu_per_container: number;
  max_memory_gb_per_container: number;
  max_disk_gb_per_container: number;
  reserved_memory_gb: number;
  reserved_disk_gb: number;
  allow_port_mapping: boolean;
  max_ports_per_container: number;
  scheduler_weight: number;
  labels: string[];
  wol_mac: string;
  wol_broadcast: string;
  ssh_user: string;
  ssh_port: number;
  sync_ip: string;
  sync_ssh_port: number;
  resource_cache_base: string;
  cpu_model: string;
  cpu_total: number;
  cpu_cores: number;
  cpu_sockets: number;
  cpu_temperature_c: number;
  memory_total_gb: number;
  disk_total_gb: number;
  cpu_used: number;
  memory_used_gb: number;
  disk_used_gb: number;
  last_seen: number;
  load_avg: number;
  cpu_usage_percent: number;
  swap_total_gb: number;
  swap_used_gb: number;
  os_version: string;
  kernel_version: string;
  driver_version: string;
  cuda_driver_api_version: string;
  incus_status: string;
  agent_version: string;
  uptime_seconds: number;
  agent_update_channel: "stable" | "canary";
  agent_auto_update: boolean;
  target_agent_version: string;
  agent_update_status: string;
  agent_update_error: string;
  agent_update_at: number;
  registered_at: number;
  gpus: Gpu[];
}

export interface NodeHardware {
  id: number;
  hostname: string;
  status: string;
  cpu_model: string;
  cpu_total: number;
  cpu_cores: number;
  cpu_sockets: number;
  cpu_temperature_c: number;
  memory_total_gb: number;
  disk_total_gb: number;
  cpu_used: number;
  memory_used_gb: number;
  disk_used_gb: number;
  last_seen: number;
  uptime_seconds: number;
  load_avg: number;
  cpu_usage_percent: number;
  swap_total_gb: number;
  swap_used_gb: number;
  cuda_driver_api_version: string;
  containers_running: number;
  containers_total: number;
  gpus: Array<Pick<Gpu, "id" | "slot" | "model" | "vram_gb" | "vram_used_mb">>;
}

export interface Image {
  id: string;
  name: string;
  cuda_major: number;
  compatible_pools: string;
  incus_ref: string;
  enabled: boolean;
  preferred: boolean;
  owner: string;
  created_at: number;
  updated_at: number;
}

export interface IncusImage {
  node_id: number;
  node: string;
  node_status: string;
  fingerprint: string;
  aliases: string;
  description: string;
  architecture: string;
  updated_at: number;
}

export interface ImageCatalog {
  images: Image[];
  incus_images: IncusImage[];
}

export interface Container {
  id: number;
  name: string;
  owner_id: number;
  node_id: number;
  image_id: string;
  image_name: string;
  owner: string;
  node: string;
  node_ip: string;
  status: string;
  access_status: "pending" | "ready" | "failed" | string;
  access_error: string;
  cpu_cores: number;
  memory_gb: number;
  disk_gb: number;
  ssh_username: string;
  ip: string;
  mounts: string[];
  created_at: number;
  updated_at: number;
  gpus: Gpu[];
  ports: ContainerPort[];
}

export interface ContainerPort {
  id: number;
  container_id: number;
  name: string;
  protocol: "tcp" | "udp";
  container_port: number;
  host_port: number;
  node_port?: number;
  public_port?: number;
  node_listen_port?: number;
  created_at: number;
  updated_at: number;
}

export interface ContainerTask {
  id: number;
  node_id: number;
  container_id: number;
  type: string;
  status: string;
  attempts: number;
  error: string;
  result: { output?: string; status?: string };
  created_at: number;
  claimed_at: number;
  finished_at: number;
  updated_at: number;
}

export interface User {
  id: number | null;
  username: string;
  display_name: string;
  role: string;
  phone: string;
  email: string;
  group_name: "platform_admin" | "admin" | "member" | "guest";
  enabled: boolean;
  ssh_key: string;
  cpu_cores: number | null;
  memory_gb: number | null;
  disk_gb: number | null;
  container_disk_limit_gb: number | null;
  storage_quota_gb: number | null;
  gpu_count: number | null;
  container_count: number | null;
  allowed_node_ids: number[];
  /** Casdoor 已注册但未登录平台的待审用户 */
  pending_sso?: boolean;
  casdoor_id?: string | null;
}

export interface SshKey {
  id: number;
  label: string;
  public_key: string;
  expires_at: number;  // 0 = 永久有效
  created_at: number;
}

export interface QuotaProfile {
  group_name: "platform_admin" | "admin" | "member" | "guest";
  role: "admin" | "member";
  cpu_cores: number; memory_gb: number; disk_gb: number; container_disk_limit_gb: number; storage_quota_gb: number; gpu_count: number; container_count: number;
  allowed_node_ids: number[];
  updated_at: number;
}

export interface UserDataPolicy {
  user_id: number;
  username: string;
  display_name: string;
  home_path: string;
  backup_enabled: boolean;
  sync_on_create: boolean;
  sync_on_stop: boolean;
  backup_interval_hours: number;
  last_backup_at: number;
  updated_at: number;
  zfs_mountpoint: string | null;
  zfs_status: string | null;
}

export interface UserDirectoryEntry {
  name: string;
  type: "file" | "directory" | "symlink";
  size_bytes: number;
  mtime: number;
  mode: string;
}

export interface SharedResourcePreview {
  kind: "text" | "image" | "video" | "pdf" | "too_large" | "unsupported";
  name: string;
  relative_path: string;
  size_bytes: number;
  mime?: string;
  text?: string;
  data?: string;
  encoding?: "hex";
  message?: string;
}

export interface UserDirectoryScan {
  user_id: number;
  relative_path: string;
  status: "unknown" | "scanning" | "ready" | "failed";
  file_count: number;
  size_bytes: number;
  entries: UserDirectoryEntry[];
  truncated: boolean;
  error: string;
  scanned_at: number;
}

export interface UserUploadResult {
  ok: boolean;
  count: number;
  bytes: number;
  scan?: UserDirectoryScan;
}

export interface SharedResource {
  id: number;
  resource_type: "dataset" | "huggingface_model" | "pytorch_model";
  name: string;
  version: string;
  source_path: string;
  mount_path: string;
  tags: string[];
  readonly: boolean;
  sync_policy: "manual" | "on_create" | "prewarm";
  enabled: boolean;
  size_bytes: number;
  file_count: number;
  check_status: "unknown" | "checking" | "ok" | "failed" | string;
  check_error: string;
  checked_at: number;
  created_at: number;
  updated_at: number;
  source_url: string;
  request_status: string;
  requested_by: number | null;
  download_progress: {
    phase?: "downloading" | "uploading" | "done" | "error";
    pct?: number;
    current_file?: string;
    files_done?: number;
    files_total?: number;
    bytes_done?: number;
    bytes_total?: number;
  } | null;
}

export interface DataSyncTask {
  id: number;
  task_type: string;
  user_id: number | null;
  resource_id: number | null;
  target_node_id: number | null;
  container_id: number | null;
  source_path: string;
  target_path: string;
  status: string;
  detail: Record<string, unknown>;
  progress?: {
    phase?: string;
    pct?: number;
    bytes_done?: number;
    bytes_total?: number;
    rate?: string;
    current_file?: string;
  } | null;
  created_at: number;
  updated_at: number;
  finished_at: number;
  last_error?: string;
  result?: Record<string, unknown>;
  username?: string;
  resource_name?: string;
  source_node?: string;
  target_node?: string;
  container_name?: string;
}

export interface ContainerSyncRule {
  id: number;
  container_id: number;
  rule_type: "scheduled_upload" | "realtime_sync" | "resource_pull" | string;
  direction: "container_to_storage" | "storage_to_container";
  name: string;
  container_path: string;
  storage_relative_path: string;
  resource_id: number | null;
  interval_minutes: number;
  schedule_kind: "daily" | "weekly" | "monthly";
  schedule_time_seconds: number;
  enabled: boolean;
  conflict_policy: "overwrite" | "skip";
  last_run_at: number;
  created_at: number;
  updated_at: number;
}

export interface ContainerSyncResponse {
  sync_task: DataSyncTask;
  node_task: ContainerTask;
}

export interface StorageVolume {
  node_id: number;
  hostname: string;
  ip: string;
  node_type: "compute" | "storage" | "app" | "mixed";
  node_status: string;
  last_seen: number;
  volume_name: "root" | "users" | "datasets" | "models" | "backups" | string;
  path: string;
  exists: boolean;
  total_gb: number;
  used_gb: number;
  free_gb: number;
  directory_used_gb: number;
  status: "ok" | "warning" | "missing" | "error" | "unknown";
  error: string;
  updated_at: number;
}

export interface StorageImageFile {
  id: number;
  source_node_id: number;
  owner_id?: number | null;
  owner?: string;
  source_node: string;
  source_node_status: string;
  fingerprint: string;
  aliases: string;
  alias: string;
  description: string;
  architecture: string;
  export_dir: string;
  base_name: string;
  size_bytes: number;
  status: "pending" | "exported" | "failed" | string;
  last_error: string;
  exported_at: number;
  created_at: number;
  updated_at: number;
}

interface StorageImageInventory {
  node_id: number;
  node: string;
  node_type: "storage" | "mixed" | string;
  node_status: string;
  fingerprint: string;
  aliases: string;
  description: string;
  architecture: string;
  updated_at: number;
}

export interface StorageImageCatalog {
  files: StorageImageFile[];
  inventory: StorageImageInventory[];
}

export interface JoinToken {
  id: number;
  token_preview: string;
  expected_hostname: string;
  note: string;
  status: string;
  node_id: number | null;
  created_by: string;
  created_at: number;
  expires_at: number;
  used_at: number;
  server_url: string;
}

export interface JoinTokenResult extends JoinToken {
  token: string;
  server_url: string;
  command: string;
  env_file: string;
}

export interface AgentRelease {
  version: string;
  architecture: "amd64" | "arm64";
  channel: "stable" | "canary";
  sha256: string;
  size_bytes: number;
  enabled: boolean;
  created_at: number;
  changelog: string;
}

export interface UserPreference<T = Record<string, unknown>> {
  key: string;
  value: T;
  updated_at: number;
}

export function getSummary() {
  return request<Summary>("/api/summary");
}

export function getNodes() {
  return request<Node[]>("/api/nodes");
}

export function getNodeHardware() {
  return request<NodeHardware[]>("/api/metrics/node-hardware");
}

export interface MetricsSnapshot {
  sampled_at: number;
  cpu_pct: number;
  memory_pct: number;
  disk_pct: number;
  gpu_avg_pct: number;
  gpu_avg_vram_pct: number;
  temperature_c: number;
}

export function getNodeMetricsHistory(nodeId: number, hours: number) {
  return request<MetricsSnapshot[]>(`/api/metrics/nodes/${nodeId}/history?hours=${hours}`);
}

export function getAgentReleases() {
  return request<AgentRelease[]>("/api/agent-releases");
}

export function buildAgentRelease(version: string, channel: "stable" | "canary", changelog: string) {
  return request<AgentRelease>(
    `/api/agent-releases/${encodeURIComponent(version)}/build?architecture=amd64&channel=${channel}`,
    { method: "POST", body: JSON.stringify({ changelog }) }
  );
}

export function deleteAgentRelease(version: string) {
  return request<{ ok: boolean }>(
    `/api/agent-releases/${encodeURIComponent(version)}?architecture=amd64`,
    { method: "DELETE" }
  );
}

export function configureAgentUpdate(id: number, payload: { channel: "stable" | "canary"; auto_update: boolean; target_version: string }) {
  return request<{ ok: boolean }>(`/api/nodes/${id}/agent-update`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export function triggerAgentUpdate(id: number) {
  return postJson<{ id: number }>(`/api/nodes/${id}/trigger-agent-update`, {});
}

export type NodeConfigPayload = NodeConfigInput;

export function updateNodeConfig(id: number, payload: NodeConfigPayload) {
  return request<Node>(`/api/nodes/${id}/config`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export function getNodeSshPubkey() {
  return request<{ pubkey: string }>("/api/nodes/ssh-pubkey");
}

export function installNodeSshPubkey(id: number) {
  return request<{ id: number }>(`/api/nodes/${id}/install-ssh-pubkey`, { method: "POST" });
}

export function nodeAction(id: number, action: "shutdown" | "reboot" | "wake") {
  return postJson<ContainerTask | { ok: boolean; node_id: number }>(`/api/nodes/${id}/${action}`, {});
}

export function deleteNode(id: number) {
  return request<{ ok: boolean; node_id: number }>(`/api/nodes/${id}`, { method: "DELETE" });
}

export function getGpus() {
  return request<Gpu[]>("/api/gpus");
}

export function getImages() {
  return request<Image[]>("/api/images");
}

export function getImageCatalog() {
  return request<ImageCatalog>("/api/image-catalog");
}

export interface UbuntuRemoteImage {
  key: string;
  os: string;
  release: string;
  version: string;
  arch: string;
  variant: string;
  aliases: string[];
  incus_ref: string;
  latest_serial: string;
}

export function getUbuntuRemoteImages() {
  return request<UbuntuRemoteImage[]>("/api/image-catalog/ubuntu-remotes");
}

export function pullImageToNode(incus_ref: string, node_id: number) {
  return postJson<{ task_ids: number[]; node_count: number }>("/api/image-catalog/pull-to-nodes", { incus_ref, node_id });
}

export function deleteNodeImage(node_id: number, image_ref: string) {
  return postJson<{ task_id: number }>("/api/image-catalog/delete-node-image", { node_id, image_ref });
}

export function copyLocalImage(image_ref: string, target_node_id: number) {
  return postJson<{ ok: boolean; alias: string; source_node: string; target_node: string; message: string }>(
    "/api/image-catalog/copy-local-image",
    { image_ref, target_node_id },
  );
}

export function saveImage(payload: Partial<Image> & Pick<Image, "id" | "name" | "incus_ref">) {
  return postJson<Image>("/api/images", payload);
}

export function deleteImage(id: string) {
  return request<Image | { ok: boolean }>(`/api/images/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function getContainers() {
  return request<Container[]>("/api/containers");
}

export function getUsers() {
  return request<User[]>("/api/users");
}

export function getMe() {
  return request<
    User & {
      quota: { cpu_cores: number; memory_gb: number; disk_gb: number; container_disk_limit_gb: number; storage_quota_gb: number; gpu_count: number; container_count: number };
      usage: Record<string, number>;
      allowed_node_ids: number[] | null;
    }
  >("/api/me");
}

export function updateProfile(payload: { display_name: string; phone: string }) {
  return request<User>("/api/me", { method: "PUT", body: JSON.stringify(payload) });
}

export function changePassword(payload: { current_password: string; new_password: string }) {
  return request<{ ok: boolean }>("/api/me/password", { method: "PUT", body: JSON.stringify(payload) });
}

export function getSshKeys() {
  return request<SshKey[]>("/api/me/ssh-keys");
}

export interface ApiToken {
  id: number;
  name: string;
  token_preview: string;
  expires_at: number;
  last_used_at: number;
  created_at: number;
}

export function getApiTokens() {
  return request<ApiToken[]>("/api/me/api-tokens");
}

export function createApiToken(payload: { name?: string; expires_at?: number }) {
  return postJson<{ id: number; token: string; preview: string; name: string; expires_at: number }>("/api/me/api-tokens", payload);
}

export function deleteApiToken(id: number) {
  return request<void>(`/api/me/api-tokens/${id}`, { method: "DELETE" });
}

export function addSshKey(payload: { label: string; public_key: string; expires_at: number }) {
  return request<SshKey>("/api/me/ssh-keys", { method: "POST", body: JSON.stringify(payload) });
}

export function deleteSshKey(id: number) {
  return request<void>(`/api/me/ssh-keys/${id}`, { method: "DELETE" });
}

export function syncSshKeysToContainers() {
  return request<{ task_ids: number[]; container_count: number }>("/api/me/ssh-keys/sync-to-containers", { method: "POST" });
}

export function saveUser(payload: Record<string, unknown>, id?: number | null) {
  return request<User>(id ? `/api/users/${id}` : "/api/users", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
}

export function approveSsoUser(casdoor_id: string, username: string, display_name: string, email: string, group_name: string) {
  return request<User>("/api/users/approve-sso", {
    method: "POST",
    body: JSON.stringify({ casdoor_id, username, display_name, email, group_name }),
  });
}

export function removeUser(id: number) {
  return request<void>(`/api/users/${id}`, { method: "DELETE" });
}

export function getQuotaProfiles() { return request<QuotaProfile[]>("/api/quota-profiles"); }
export function saveQuotaProfile(group: string, payload: Omit<QuotaProfile, "group_name" | "updated_at">) {
  return request<QuotaProfile>(`/api/quota-profiles/${group}`, { method: "PUT", body: JSON.stringify(payload) });
}

export function login(username: string, password: string) {
  return postJson<{ token: string; expires_at: number; user: import("../auth").AuthUser }>("/api/auth/login", { username, password });
}
export function registerPlatformUser(payload: { username: string; email: string; password: string }) {
  return postJson<RegisterResult>("/api/auth/register", payload);
}
export function logout() { return postJson<{ ok: boolean }>("/api/auth/logout", {}); }

export function getUserDataPolicies() {
  return request<UserDataPolicy[]>("/api/data/user-policies");
}

export function getUserDirectory(userId: number, relativePath = "") {
  return request<UserDirectoryScan>(`/api/storage/users/${userId}/files?relative_path=${encodeURIComponent(relativePath)}`);
}

export function getUserDirectoryLive(userId: number, relativePath = "") {
  return request<UserDirectoryScan>(`/api/storage/users/${userId}/files/live?relative_path=${encodeURIComponent(relativePath)}`);
}

export function scanUserDirectory(userId: number, relativePath = "") {
  return postJson<ContainerTask>(`/api/storage/users/${userId}/files/scan`, { relative_path: relativePath, limit: 500 });
}

export function uploadUserFiles(userId: number, relativePath: string, files: File[], paths: string[], onProgress?: (progress: { loaded: number; total: number; percent: number }) => void) {
  const form = new FormData();
  form.append("relative_path", relativePath);
  files.forEach((file, index) => {
    form.append("files", file, file.name);
    form.append("paths", paths[index] || file.name);
  });
  if (onProgress) {
    return new Promise<UserUploadResult>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/storage/users/${userId}/upload`);
      if (authToken.value) xhr.setRequestHeader("Authorization", `Bearer ${authToken.value}`);
      xhr.upload.onprogress = (event) => {
        const total = event.total || files.reduce((sum, file) => sum + file.size, 0);
        const loaded = event.loaded || 0;
        onProgress({ loaded, total, percent: total ? Math.min(100, Math.round((loaded / total) * 100)) : 0 });
      };
      xhr.onload = () => {
        let data: any = null;
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch { data = { detail: xhr.responseText }; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data as UserUploadResult);
        else reject(new Error(translateApiError(data?.detail || data?.error || "上传失败")));
      };
      xhr.onerror = () => reject(new Error(translateApiError("上传失败，请检查网络连接")));
      xhr.send(form);
    });
  }
  return request<UserUploadResult>(`/api/storage/users/${userId}/upload`, { method: "POST", body: form });
}
export function previewUserFile(userId: number, relativePath = "") {
  return request<SharedResourcePreview>(`/api/storage/users/${userId}/preview?relative_path=${encodeURIComponent(relativePath)}`);
}

export function deleteUserFile(userId: number, relativePath: string) {
  return request<void>(`/api/storage/users/${userId}/file?relative_path=${encodeURIComponent(relativePath)}`, { method: "DELETE" });
}

export function getSharedResources() {
  return request<SharedResource[]>("/api/data/shared-resources");
}

export interface StorageSettings {
  dataset_base_path: string;
  model_base_path: string;
  user_base_path: string;
  hf_endpoint: string;
  hf_endpoint_enabled: boolean;
}

export interface UserStorageDataset {
  user_id: number;
  username: string;
  display_name: string;
  enabled: boolean;
  home_path: string;
  storage_quota_gb: number;
  node_id: number | null;
  node: string;
  node_status: string;
  dataset_name: string;
  mountpoint: string;
  quota_gb: number;
  status: string;
  last_error: string;
  applied_at: number;
  updated_at: number;
}

export interface UserWorkspaceVolume {
  user_id: number;
  username: string;
  display_name: string;
  enabled: boolean;
  node_id: number;
  node: string;
  node_status: string;
  volume_name: string;
  quota_gb: number;
  used_gb: number | null;
  status: string;
  last_error: string;
  active_container_count: number;
  created_at: number;
  updated_at: number;
  removed_at: number;
}

export function getResourceTagOptions() {
  return request<string[]>("/api/data/resource-tag-options");
}

export function getStorageSettings() {
  return request<StorageSettings>("/api/data/storage-settings");
}

export function getUserStorageDatasets() {
  return request<UserStorageDataset[]>("/api/storage/user-datasets");
}

export function ensureUserStorageDataset(userId: number) {
  return postJson<{ task: ContainerTask | null }>(`/api/storage/user-datasets/${userId}/ensure`, {});
}

export function ensureAllUserStorageDatasets() {
  return postJson<{ tasks: ContainerTask[] }>("/api/storage/user-datasets/ensure-all", {});
}

export function removeUserStorageDataset(userId: number) {
  return request<{ task: ContainerTask | null }>(`/api/storage/user-datasets/${userId}`, { method: "DELETE" });
}

export function getUserWorkspaceVolumes(fetchDiskUsage = false) {
  const url = fetchDiskUsage ? "/api/storage/workspace-volumes?fetch_disk_usage=true" : "/api/storage/workspace-volumes";
  return request<UserWorkspaceVolume[]>(url);
}

export function removeUserWorkspaceVolume(nodeId: number, userId: number, confirmVolumeName: string) {
  return postJson<{ task: ContainerTask | null }>(`/api/storage/workspace-volumes/${nodeId}/${userId}/remove`, { confirm_volume_name: confirmVolumeName });
}

export function updateStorageSettings(payload: StorageSettings) {
  return request<StorageSettings>("/api/data/storage-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function updateSharedResourceInfo(id: number, payload: { name: string; version: string; tags: string[] }) {
  return patchJson<SharedResource>(`/api/data/shared-resources/${id}/info`, payload);
}

export function requestSharedResource(payload: { resource_type: SharedResource["resource_type"]; name: string; version: string; source: string; tags: string[]; hf_repo_id: string; hf_revision: string; hf_token: string; ms_repo_id: string; ms_revision: string; ms_token: string }) {
  return postJson<{ resource: SharedResource; task: ContainerTask }>("/api/data/resource-requests", payload);
}

export function verifySharedResource(id: number) {
  return postJson<ContainerTask>(`/api/data/shared-resources/${id}/verify`, {});
}

export function deleteSharedResource(id: number) {
  return request<void>(`/api/data/shared-resources/${id}`, { method: "DELETE" });
}

export function getSharedResourceFiles(id: number, relative_path = "") {
  return request<UserDirectoryScan & { resource_id: number }>(`/api/data/shared-resources/${id}/files?relative_path=${encodeURIComponent(relative_path)}`);
}

export function scanSharedResource(id: number, relative_path = "") {
  return request<ContainerTask>(`/api/data/shared-resources/${id}/files/scan?relative_path=${encodeURIComponent(relative_path)}`, { method: "POST" });
}

export function previewSharedResourceFile(id: number, relative_path = "") {
  return request<SharedResourcePreview>(`/api/data/shared-resources/${id}/preview?relative_path=${encodeURIComponent(relative_path)}`);
}

export function getStorageVolumes() {
  return request<StorageVolume[]>("/api/storage/volumes");
}

export function getStorageImages() {
  return request<StorageImageCatalog>("/api/storage/images");
}

export function distributeStorageImage(id: number, target_node_ids: number[] = []) {
  return postJson<{ tasks: ContainerTask[] }>(`/api/storage/images/${id}/distribute`, { target_node_ids });
}

export function deleteStorageImage(id: number) {
  return request<void>(`/api/storage/images/${id}`, { method: "DELETE" });
}

export interface NodeResourceCache {
  node_id: number;
  resource_id: number;
  status: "pending" | "syncing" | "ready" | "failed";
  local_path: string | null;
  synced_at: number | null;
  size_bytes: number | null;
  error: string | null;
  updated_at: number;
  hostname: string;
  node_status: string;
  resource_name: string;
  resource_type: string;
  version: string;
}

export function getResourceCacheMatrix() {
  return request<NodeResourceCache[]>("/api/storage/resource-cache");
}

export function triggerResourceSync(resourceId: number, nodeId: number) {
  return postJson<ContainerTask>(`/api/storage/resources/${resourceId}/sync-to-node/${nodeId}`, {});
}

export function triggerResourceSyncAllNodes(resourceId: number) {
  return postJson<{ tasks: ContainerTask[]; node_count: number }>(`/api/storage/resources/${resourceId}/sync-to-all-nodes`, {});
}

export function syncContainerNodeCache(containerId: number, resourceIds: number[]) {
  return postJson<{ tasks: ContainerTask[]; submitted_count: number; skipped_resource_ids: number[] }>(
    `/api/containers/${containerId}/sync-node-cache`,
    { resource_ids: resourceIds },
  );
}

export function clearResourceCache(nodeId: number, resourceId: number) {
  return request<void>(`/api/storage/resource-cache/${nodeId}/${resourceId}`, { method: "DELETE" });
}

export function getJoinTokens() {
  return request<JoinToken[]>("/api/node-join-tokens");
}

export function deleteJoinToken(id: number) {
  return request<{ ok: boolean }>(`/api/node-join-tokens/${id}`, { method: "DELETE" });
}

export function getUserPreference<T = Record<string, unknown>>(key: string) {
  return request<UserPreference<T>>(`/api/me/preferences/${encodeURIComponent(key)}`);
}

export function updateUserPreference<T = Record<string, unknown>>(key: string, value: T) {
  return request<UserPreference<T>>(`/api/me/preferences/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify({ value })
  });
}

export function createJoinToken(payload: {
  expected_hostname: string;
  server_url: string;
  expires_in_hours: number;
  note: string;
}) {
  return postJson<JoinTokenResult>("/api/node-join-tokens", payload);
}

export function createContainer(payload: ContainerCreateInput) {
  return postJson<Container>("/api/containers", payload);
}

export function containerAction(id: number, action: "start" | "stop" | "restart") {
  return postJson<Container>(`/api/containers/${id}/${action}`, {});
}
export function retryContainer(id: number) {
  return postJson<Container>(`/api/containers/${id}/retry`, {});
}
export function retryContainerSshAccess(id: number) {
  return postJson<{ task: ContainerTask; container: Container }>(`/api/containers/${id}/ssh-access/retry`, {});
}

export function updateContainerResources(id: number, payload: { cpu_cores: number; memory_gb: number; gpu_count: number; gpu_model?: string }) {
  return request<{ task_id: number; container: Container }>(`/api/containers/${id}/resources`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteContainer(id: number, name: string, force = false) {
  return request<Container | { ok: boolean; container_id: number }>(`/api/containers/${id}`, {
    method: "DELETE",
    body: JSON.stringify({ name, force })
  });
}

export function publishContainerImage(containerId: number, payload: { alias: string; display_name?: string; register_platform?: boolean; export_to_storage?: boolean }) {
  return postJson<ContainerTask>(`/api/containers/${containerId}/publish-image`, payload);
}

export function createContainerPort(containerId: number, payload: { name: string; protocol: string; container_port: number }) {
  return postJson<ContainerPort>(`/api/containers/${containerId}/ports`, payload);
}

export function updateContainerPort(
  containerId: number,
  portId: number,
  payload: { name: string; protocol: string; container_port: number }
) {
  return request<ContainerPort>(`/api/containers/${containerId}/ports/${portId}`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export function deleteContainerPort(containerId: number, portId: number) {
  return request<{ ok: boolean }>(`/api/containers/${containerId}/ports/${portId}`, { method: "DELETE" });
}

export function getContainerSyncTasks(containerId: number) {
  return request<DataSyncTask[]>(`/api/containers/${containerId}/sync-tasks`);
}

export interface RecentTask {
  id: number;
  kind: "node_task" | "sync_task";
  container_id: number | null;
  container_name: string;
  type: string;
  status: string;
  error: string;
  created_at: number;
  updated_at: number;
  finished_at: number;
}

export function getRecentTasks(page = 1, perPage = 20, statusGroup = "") {
  const q = statusGroup ? `&status_group=${encodeURIComponent(statusGroup)}` : "";
  return request<{ total: number; items: RecentTask[] }>(`/api/tasks/recent?page=${page}&per_page=${perPage}${q}`);
}

export function runContainerSync(containerId: number, payload: {
  direction: "storage_to_container" | "container_to_storage";
  storage_type: "dataset" | "model" | "user_file";
  resource_id?: number | null;
  storage_relative_path: string;
  container_path: string;
  conflict_policy: "overwrite" | "skip";
}) {
  return postJson<ContainerSyncResponse>(`/api/containers/${containerId}/sync`, payload);
}

export function getContainerSyncRules(containerId: number) {
  return request<ContainerSyncRule[]>(`/api/containers/${containerId}/sync-rules`);
}

export function saveContainerSyncRule(containerId: number, payload: ContainerSyncRuleInput, ruleId?: number) {
  return request<ContainerSyncRule>(
    ruleId ? `/api/containers/${containerId}/sync-rules/${ruleId}` : `/api/containers/${containerId}/sync-rules`,
    { method: ruleId ? "PUT" : "POST", body: JSON.stringify(payload) }
  );
}

export function deleteContainerSyncRule(containerId: number, ruleId: number) {
  return request<void>(`/api/containers/${containerId}/sync-rules/${ruleId}`, { method: "DELETE" });
}

export function runContainerSyncRule(containerId: number, ruleId: number) {
  return postJson<ContainerSyncResponse>(`/api/containers/${containerId}/sync-rules/${ruleId}/run`, {});
}
