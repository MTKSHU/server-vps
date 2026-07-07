<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { FolderOpened, Edit, RefreshRight, CircleCheck, Delete, Refresh, Download, View, Setting, DataLine, Connection, Upload, FolderAdd, Close, Select } from "@element-plus/icons-vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox, ElNotification } from "element-plus";
import StatusTag from "../components/StatusTag.vue";
import {
  deleteSharedResource, deleteUserFile, getMe,
  getResourceTagOptions,
  getSharedResourceFiles, getSharedResources, getStorageSettings, getStorageVolumes, getUserDataPolicies,
  getUserDirectory, getUserDirectoryLive, getUserStorageDatasets, getUserWorkspaceVolumes, requestSharedResource, scanSharedResource, scanUserDirectory,
  previewSharedResourceFile, previewUserFile,
  ensureAllUserStorageDatasets, ensureUserStorageDataset, removeUserStorageDataset, removeUserWorkspaceVolume,
  updateSharedResourceInfo, updateStorageSettings, uploadUserFiles, verifySharedResource,
  getResourceCacheMatrix, triggerResourceSync, clearResourceCache,
  triggerResourceSyncAllNodes,
  type SharedResource, type SharedResourcePreview, type StorageSettings, type StorageVolume, type UserDataPolicy,
  type UserDirectoryEntry, type UserDirectoryScan, type UserStorageDataset, type UserWorkspaceVolume,
  type NodeResourceCache,
} from "../api/cluster";
import { authToken, authUser, hasAdminAccess } from "../auth";

const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const loading = ref(false);
const resources = ref<SharedResource[]>([]);
const volumes = ref<StorageVolume[]>([]);
const userDatasets = ref<UserStorageDataset[]>([]);
const workspaceVolumes = ref<UserWorkspaceVolume[]>([]);
const zfsEnsuring = ref(false);
const zfsBusyUserIds = ref<number[]>([]);
const workspaceLoading = ref(false);
const workspaceBusyKeys = ref<string[]>([]);
const cacheMatrix = ref<NodeResourceCache[]>([]);
const cacheLoading = ref(false);
const cacheBusyKeys = ref<string[]>([]);
const cacheLoaded = ref(false);
const cacheStatusFilter = ref("");
const cacheNodeFilter = ref("");
const policy = ref<UserDataPolicy | null>(null);
const directory = ref<UserDirectoryScan | null>(null);
const homeDirectory = ref<UserDirectoryScan | null>(null);
const currentPath = ref("");
const fileInputRef = ref<HTMLInputElement | null>(null);
const folderInputRef = ref<HTMLInputElement | null>(null);
const uploading = ref(false);
const dirLoading = ref(false);
const uploadProgress = reactive({ loaded: 0, total: 0, percent: 0 });
const storageQuotaGb = ref(0);
const activeTab = ref(typeof route.query.tab === "string" ? route.query.tab : "files");
const requestVisible = ref(false);
const requesting = ref(false);
const requestForm = reactive({ resource_type: "dataset" as SharedResource["resource_type"], name: "", version: "default", tags: [] as string[], source: "modelscope" as "huggingface" | "modelscope", hf_repo_id: "", hf_revision: "main", hf_token: "", ms_repo_id: "", ms_revision: "master", ms_token: "" });
const tagLibrary = ref<string[]>([]);
type TagGroup = { label: string; options: { label: string; value: string }[] };
function buildTagGroups(tags: string[]): TagGroup[] {
  const groups: Record<string, { label: string; value: string }[]> = {};
  for (const tag of Array.from(new Set(tags.map((item) => item.trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b, "zh"))) {
    const slash = tag.indexOf("/");
    const cat = slash > 0 ? tag.slice(0, slash) : "其他";
    const sub = slash > 0 ? tag.slice(slash + 1) : tag;
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push({ label: sub, value: tag });
  }
  return Object.entries(groups)
    .sort(([a], [b]) => (a === "其他" ? 1 : b === "其他" ? -1 : a.localeCompare(b, "zh")))
    .map(([label, options]) => ({ label, options }));
}
const resourceTagGroups = computed(() => buildTagGroups(tagLibrary.value));
const resourceTagEditorVisible = ref(false);
const resourceTagEditorForm = reactive({ id: 0, name: "", version: "", tags: [] as string[] });
const downloadingKeys = ref<string[]>([]);
function downloadKey(name = "") { return [currentPath.value, name].filter(Boolean).join("/") || "__root__"; }
function isDownloading(name = "") { return downloadingKeys.value.includes(downloadKey(name)); }

// 搜索/筛选状态
const datasetSearch = ref("");
const datasetTagFilter = ref<string[]>([]);
const modelSearch = ref("");
const modelTagFilter = ref<string[]>([]);

// 未过滤的原始列表，用于顶部统计卡片
const allDatasets = computed(() => resources.value.filter((item) => item.resource_type === "dataset"));
const allModels = computed(() => resources.value.filter((item) => item.resource_type !== "dataset"));
const datasetFilterTagGroups = computed(() => buildTagGroups(allDatasets.value.flatMap((item) => item.tags || [])));
const modelFilterTagGroups = computed(() => buildTagGroups(allModels.value.flatMap((item) => item.tags || [])));
const datasetFilterTags = computed(() => new Set(datasetFilterTagGroups.value.flatMap((group) => group.options.map((opt) => opt.value))));
const modelFilterTags = computed(() => new Set(modelFilterTagGroups.value.flatMap((group) => group.options.map((opt) => opt.value))));

watch(datasetFilterTags, (tags) => {
  datasetTagFilter.value = datasetTagFilter.value.filter((tag) => tags.has(tag));
});
watch(modelFilterTags, (tags) => {
  modelTagFilter.value = modelTagFilter.value.filter((tag) => tags.has(tag));
});

const datasets = computed(() => {
  let list = resources.value.filter((item) => item.resource_type === "dataset");
  const q = datasetSearch.value.trim().toLowerCase();
  if (q) list = list.filter(r => r.name.toLowerCase().includes(q) || (r.version || "").toLowerCase().includes(q));
  if (datasetTagFilter.value.length) {
    list = list.filter(r => datasetTagFilter.value.some(t => (r.tags || []).some(tag => tag.toLowerCase().includes(t.toLowerCase()))));
  }
  return list;
});
const models = computed(() => {
  let list = resources.value.filter((item) => item.resource_type !== "dataset");
  const q = modelSearch.value.trim().toLowerCase();
  if (q) list = list.filter(r => r.name.toLowerCase().includes(q) || (r.version || "").toLowerCase().includes(q));
  if (modelTagFilter.value.length) {
    list = list.filter(r => modelTagFilter.value.some(t => (r.tags || []).some(tag => tag.toLowerCase().includes(t.toLowerCase()))));
  }
  return list;
});
// 存储统计：used = users + datasets + models 之和；total = root 的 free + used
const storageStats = computed(() => {
  const dataVolumes = volumes.value.filter((v) => ["users", "datasets", "models"].includes(v.volume_name));
  const rootVolumes = volumes.value.filter((v) => v.volume_name === "root");
  const used_gb = dataVolumes.reduce((s, v) => s + (v.used_gb || 0), 0);
  const free_gb = rootVolumes.reduce((s, v) => s + (v.free_gb || 0), 0);
  const total_gb = used_gb + free_gb;
  const hostname = rootVolumes.map((v) => v.hostname).join(", ") || "";
  const status = rootVolumes.some((v) => v.status === "error")
    ? "error"
    : rootVolumes.some((v) => v.status === "warning")
    ? "warning"
    : rootVolumes.some((v) => v.status === "ok")
    ? "ok"
    : "unknown";
  return { used_gb, free_gb, total_gb, hostname, status };
});
// 兼容旧引用 storage（用于显示状态和节点名）
const storage = computed(() => {
  if (!storageStats.value.hostname) return volumes.value.find((item) => item.volume_name === "root") ?? null;
  return { ...storageStats.value, volume_name: "root" };
});
const isAdmin = computed(() => hasAdminAccess());
const totalResourceBytes = computed(() => resources.value.reduce((sum, item) => sum + item.size_bytes, 0));
const currentUserId = computed(() => policy.value?.user_id ?? authUser.value?.id ?? 0);
const storageQuotaBytes = computed(() => Math.max(0, storageQuotaGb.value) * 1024 * 1024 * 1024);
const homeUsageBytes = computed(() => {
  if (!currentPath.value) return directory.value?.size_bytes ?? homeDirectory.value?.size_bytes ?? 0;
  return homeDirectory.value?.size_bytes ?? 0;
});
const homeUsageText = computed(() => {
  if (!storageQuotaBytes.value) return `${bytes(homeUsageBytes.value)} / 不限`;
  return `${bytes(homeUsageBytes.value)} / ${bytes(storageQuotaBytes.value)}`;
});
const homeUsagePercent = computed(() => {
  if (!storageQuotaBytes.value) return 0;
  return Math.min(100, Math.round((homeUsageBytes.value / storageQuotaBytes.value) * 100));
});
function taskError(task: { last_error?: string; detail?: Record<string, unknown> }) { return task.last_error || String(task.detail?.last_error || ""); }

// Storage settings state
const settingsVisible = ref(false);
const settingsForm = reactive<StorageSettings>({ dataset_base_path: "/data/datasets", model_base_path: "/data/models/huggingface", user_base_path: "/data/users", hf_endpoint: "", hf_endpoint_enabled: false });
const settingsSaving = ref(false);

// Resource file browser state
const resBrowseVisible = ref(false);
const resBrowseResource = ref<SharedResource | null>(null);
const resBrowsePath = ref("");
const resBrowseScan = ref<(UserDirectoryScan & { resource_id: number }) | null>(null);
const previewVisible = ref(false);
const previewLoading = ref(false);
const previewData = ref<SharedResourcePreview | null>(null);
const previewBlobUrl = ref("");

type ResourceBrowseEntry = UserDirectoryEntry & { _virtual?: "parent" };
type UserBrowseEntry = UserDirectoryEntry & { _virtual?: "parent" };

function bytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"]; let size = value || 0; let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function resetUploadProgress() {
  uploadProgress.loaded = 0;
  uploadProgress.total = 0;
  uploadProgress.percent = 0;
}
function gbytes(used: number, total: number): string {
  // 以 total 决定单位，used/total 用相同单位显示
  const units: [number, string][] = [[1024 * 1024, "PB"], [1024, "TB"], [1, "GB"]];
  const [div, unit] = units.find(([d]) => total >= d) ?? [1, "GB"];
  const fmt = (v: number) => (v / div) >= 100 ? Math.round(v / div).toString() : (v / div).toFixed(1);
  return `${fmt(used)} / ${fmt(total)} ${unit}`;
}
function gbValue(value: number): string {
  return value >= 100 ? `${Math.round(value)} GB` : `${value.toFixed(1)} GB`;
}
function sourceLabel(row: SharedResource) {
  const url = row.source_url || "";
  if (url.startsWith("ms://")) return "ModelScope";
  if (url.startsWith("hf://")) return "HuggingFace";
  return "手动";
}
function previewable(row: UserDirectoryEntry) {
  if (row.type !== "file") return false;
  const lower = row.name.toLowerCase();
  return [".txt", ".py", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".md", ".sh", ".log", ".csv", ".xml", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".pdf"].some((ext) => lower.endsWith(ext));
}
function previewDataUrl() {
  return previewBlobUrl.value;
}
function downloadFilenameFromHeader(header: string | null, fallback: string) {
  if (!header) return fallback;
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try { return decodeURIComponent(utf8Match[1]); } catch { return utf8Match[1]; }
  }
  const quotedMatch = header.match(/filename="?([^";]+)"?/i);
  if (quotedMatch?.[1]) return quotedMatch[1];
  return fallback;
}
const resourceBrowseEntries = computed<ResourceBrowseEntry[]>(() => {
  const entries = (resBrowseScan.value?.entries || []) as ResourceBrowseEntry[];
  if (!resBrowsePath.value) return entries;
  return [{ name: "上一级", type: "directory", size_bytes: 0, mtime: 0, mode: "", _virtual: "parent" }, ...entries];
});
const userBrowseEntries = computed<UserBrowseEntry[]>(() => {
  const entries = (directory.value?.entries || []) as UserBrowseEntry[];
  if (!currentPath.value) return entries;
  return [{ name: "上一级", type: "directory", size_bytes: 0, mtime: 0, mode: "", _virtual: "parent" }, ...entries];
});
const FILE_PAGE_SIZE = 50;
const filePage = ref(1);
// 进入新目录时重置页码
watch(() => currentPath.value, () => { filePage.value = 1; });
const userBrowseEntriesPaged = computed<UserBrowseEntry[]>(() => {
  const all = userBrowseEntries.value;
  // "上一级" 虚拟项不计入分页，始终保留在首页
  const virtualItems = all.filter((e) => e._virtual);
  const realItems = all.filter((e) => !e._virtual);
  const start = (filePage.value - 1) * FILE_PAGE_SIZE;
  const pageItems = realItems.slice(start, start + FILE_PAGE_SIZE);
  return filePage.value === 1 ? [...virtualItems, ...pageItems] : pageItems;
});
function progressPhaseLabel(row: SharedResource) {
  return row.download_progress?.phase === "uploading" ? "⬆ 推送到存储节点" : "⬇ 下载中";
}
function progressPercent(row: SharedResource) {
  const pct = Number(row.download_progress?.pct || 0);
  return Math.max(0, Math.min(100, Math.round(pct)));
}
function hasDeterminateProgress(row: SharedResource) {
  return progressPercent(row) > 0;
}
function progressDetail(row: SharedResource) {
  const progress = row.download_progress || {};
  if ((progress.bytes_total || 0) > 0) return `${bytes(progress.bytes_done || 0)} / ${bytes(progress.bytes_total || 0)}`;
  if ((progress.files_total || 0) > 0) return `${progress.files_done || 0}/${progress.files_total || 0} 文件`;
  if ((progress.files_done || 0) > 0) return `已完成 ${progress.files_done || 0} 文件`;
  return progress.phase === "uploading" ? "正在统计上传进度..." : "正在收集下载进度...";
}
function formatTime(timestamp: number) { return timestamp ? new Date(timestamp * 1000).toLocaleString() : "-"; }
function zfsStatusType(status: string) {
  if (status === "applied") return "success";
  if (status === "failed") return "danger";
  if (status === "pending" || status === "removing") return "warning";
  return "info";
}
async function loadUserDatasets() {
  if (!isAdmin.value) return;
  userDatasets.value = await getUserStorageDatasets();
}

async function loadWorkspaceVolumes(fetchDiskUsage = false) {
  if (!isAdmin.value) return;
  workspaceLoading.value = true;
  try {
    workspaceVolumes.value = await getUserWorkspaceVolumes(fetchDiskUsage);
  } finally {
    workspaceLoading.value = false;
  }
}
async function refreshWorkspaceVolumesDiskUsage() {
  await loadWorkspaceVolumes(true);
}
async function ensureZfsDataset(row: UserStorageDataset) {
  zfsBusyUserIds.value.push(row.user_id);
  try {
    await ensureUserStorageDataset(row.user_id);
    ElMessage.success("ZFS dataset 任务已提交");
    await Promise.all([loadUserDatasets(), loadWorkspaceVolumes()]);
  } finally {
    zfsBusyUserIds.value = zfsBusyUserIds.value.filter((id) => id !== row.user_id);
  }
}
async function ensureAllZfsDatasets() {
  zfsEnsuring.value = true;
  try {
    const result = await ensureAllUserStorageDatasets();
    ElMessage.success(`已提交 ${result.tasks.length} 个 ZFS dataset 任务`);
    await Promise.all([loadUserDatasets(), loadWorkspaceVolumes()]);
  } finally {
    zfsEnsuring.value = false;
  }
}
async function removeZfsDataset(row: UserStorageDataset) {
  try {
    await ElMessageBox.prompt(
      `将移除用户「${row.username}」在存储节点上的 ZFS dataset。请输入用户名 ${row.username} 确认。`,
      "删除 ZFS 用户目录",
      { confirmButtonText: "删除", cancelButtonText: "取消", inputPattern: new RegExp(`^${row.username.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`), inputErrorMessage: "用户名不匹配", type: "warning" }
    );
  } catch {
    return;
  }
  zfsBusyUserIds.value.push(row.user_id);
  try {
    const result = await removeUserStorageDataset(row.user_id);
    ElMessage.success(result.task ? "ZFS dataset 删除任务已提交" : "ZFS dataset 记录已移除");
    await Promise.all([loadUserDatasets(), loadWorkspaceVolumes()]);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "ZFS dataset 删除失败");
  } finally {
    zfsBusyUserIds.value = zfsBusyUserIds.value.filter((id) => id !== row.user_id);
  }
}
function workspaceKey(row: UserWorkspaceVolume) {
  return `${row.node_id}:${row.user_id}`;
}
function workspaceBusy(row: UserWorkspaceVolume) {
  return workspaceBusyKeys.value.includes(workspaceKey(row));
}
function workspaceStatusType(status: string) {
  if (status === "active") return "success";
  if (status === "removing") return "warning";
  if (status === "failed") return "danger";
  if (status === "removed") return "info";
  return "info";
}
async function removeWorkspaceVolumeAction(row: UserWorkspaceVolume) {
  let confirmVolumeName = "";
  try {
    const value = await ElMessageBox.prompt(
      `将回收节点「${row.node}」上用户「${row.username}」的 /workspace 数据卷。请输入数据卷名 ${row.volume_name} 确认。`,
      "回收节点用户数据卷",
      { confirmButtonText: "回收", cancelButtonText: "取消", inputPattern: new RegExp(`^${row.volume_name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`), inputErrorMessage: "数据卷名不匹配", type: "warning" }
    );
    confirmVolumeName = String(value.value || "");
  } catch {
    return;
  }
  workspaceBusyKeys.value.push(workspaceKey(row));
  try {
    await removeUserWorkspaceVolume(row.node_id, row.user_id, confirmVolumeName);
    ElMessage.success("数据卷回收任务已提交");
    await loadWorkspaceVolumes();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "数据卷回收失败");
  } finally {
    workspaceBusyKeys.value = workspaceBusyKeys.value.filter((key) => key !== workspaceKey(row));
  }
}
function zfsBusy(row: UserStorageDataset) {
  return zfsBusyUserIds.value.includes(row.user_id);
}

function cacheKey(nodeId: number, resourceId: number) { return `${nodeId}:${resourceId}`; }
function cacheBusy(row: NodeResourceCache) { return cacheBusyKeys.value.includes(cacheKey(row.node_id, row.resource_id)); }
function cacheStatusType(status: string) {
  if (status === "ready") return "success";
  if (status === "syncing") return "warning";
  if (status === "pending") return "info";
  if (status === "failed") return "danger";
  return "info";
}
const cacheNodeOptions = computed(() => [...new Set(cacheMatrix.value.map((r) => r.hostname))].sort());
const filteredCacheMatrix = computed(() => {
  let list = cacheMatrix.value;
  if (cacheStatusFilter.value) list = list.filter((r) => r.status === cacheStatusFilter.value);
  if (cacheNodeFilter.value) list = list.filter((r) => r.hostname === cacheNodeFilter.value);
  return list;
});
async function loadCacheMatrix() {
  cacheLoading.value = true;
  try {
    cacheMatrix.value = await getResourceCacheMatrix();
    cacheLoaded.value = true;
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "加载节点缓存失败");
  } finally {
    cacheLoading.value = false;
  }
}
async function triggerCacheSync(row: NodeResourceCache) {
  cacheBusyKeys.value.push(cacheKey(row.node_id, row.resource_id));
  try {
    await triggerResourceSync(row.resource_id, row.node_id);
    ElMessage.success("同步任务已提交");
    await loadCacheMatrix();
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "触发同步失败");
  } finally {
    cacheBusyKeys.value = cacheBusyKeys.value.filter((k) => k !== cacheKey(row.node_id, row.resource_id));
  }
}
async function clearCacheRecord(row: NodeResourceCache) {
  try {
    await ElMessageBox.confirm(
      `确认清除节点「${row.hostname}」上「${row.resource_name}」的缓存记录？此操作仅删除数据库记录，不会删除节点上的实际文件。`,
      "清除缓存记录",
      { type: "warning", confirmButtonText: "清除", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  cacheBusyKeys.value.push(cacheKey(row.node_id, row.resource_id));
  try {
    await clearResourceCache(row.node_id, row.resource_id);
    cacheMatrix.value = cacheMatrix.value.filter((r) => !(r.node_id === row.node_id && r.resource_id === row.resource_id));
    ElMessage.success("缓存记录已清除");
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "清除缓存失败");
  } finally {
    cacheBusyKeys.value = cacheBusyKeys.value.filter((k) => k !== cacheKey(row.node_id, row.resource_id));
  }
}

async function load() {
  loading.value = true;
  try {
    const [resourceRows, volumeRows, me, settings, tagOpts] = await Promise.all([getSharedResources(), getStorageVolumes(), getMe(), getStorageSettings(), getResourceTagOptions()]);
    tagLibrary.value = tagOpts;
    resources.value = resourceRows; volumes.value = volumeRows;
    storageQuotaGb.value = Number(me.quota?.storage_quota_gb || 0);
    Object.assign(settingsForm, settings);
    try {
      const policies = await getUserDataPolicies();
      policy.value = policies.find((item) => item.user_id === me.id) || null;
    } catch {
      policy.value = null;
    }
    if (currentUserId.value) {
      // 先用缓存结果（纯查库，毫秒级）快速渲染首屏，再后台用 live-ls 更新为最新，
      // 避免每次进入"我的文件"都要等待到存储节点的 SSH 往返。
      try {
        const cached = await getUserDirectory(currentUserId.value, currentPath.value);
        if (cached && cached.status !== "unknown") { directory.value = cached; if (!currentPath.value) homeDirectory.value = cached; }
      } catch { /* 无缓存则忽略，交给 live-ls */ }
      dirLoading.value = true;
      void loadDirectory().then(() => {
        if (directory.value?.status === "unknown" || (directory.value?.status === "ready" && directory.value.entries.length === 0 && directory.value.error)) void refreshDirectory();
      }).catch(() => { /* 静默 */ }).finally(() => { dirLoading.value = false; });
    }
    await Promise.all([loadUserDatasets(), loadWorkspaceVolumes()]);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "加载失败，请刷新页面重试");
  } finally { loading.value = false; }
}
// 仅刷新共享资源列表（下载进度轮询用），不触发全页 loading
async function pollResources() {
  try { resources.value = await getSharedResources(); } catch { /* ignore */ }
}
async function openSettings() {
  const settings = await getStorageSettings();
  Object.assign(settingsForm, settings);
  settingsVisible.value = true;
}
async function saveSettings() {
  settingsSaving.value = true;
  try {
    const result = await updateStorageSettings(settingsForm);
    Object.assign(settingsForm, result);
    settingsVisible.value = false;
    ElMessage.success("存储设置已保存");
  } finally { settingsSaving.value = false; }
}
async function submitRequest() {
  requesting.value = true;
  try {
    await requestSharedResource(requestForm);
    requestVisible.value = false;
    Object.assign(requestForm, { name: "", version: "default", tags: [], hf_repo_id: "", hf_revision: "main", hf_token: "", ms_repo_id: "", ms_revision: "master", ms_token: "" });
    ElMessage.success("已在后台开始下载，稍后刷新查看状态");
    await load();
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "提交失败，请重试");
  } finally {
    requesting.value = false;
  }
}
async function checkResource(row: SharedResource) { try { await verifySharedResource(row.id); ElMessage.success("校验任务已提交"); } catch (err) { ElMessage.error(err instanceof Error ? err.message : "操作失败"); } }
function redownload(row: SharedResource) {
  const url = row.source_url || "";
  let source: "huggingface" | "modelscope" = "modelscope";
  let hf_repo_id = "", hf_revision = "main", ms_repo_id = "", ms_revision = "master";
  if (url.startsWith("hf://")) {
    source = "huggingface";
    const rest = url.slice(5);
    const at = rest.lastIndexOf("@");
    hf_repo_id = at >= 0 ? rest.slice(0, at) : rest;
    hf_revision = at >= 0 ? rest.slice(at + 1) : "main";
  } else if (url.startsWith("ms://")) {
    source = "modelscope";
    const rest = url.slice(5);
    const at = rest.lastIndexOf("@");
    ms_repo_id = at >= 0 ? rest.slice(0, at) : rest;
    ms_revision = at >= 0 ? rest.slice(at + 1) : "master";
  }
  Object.assign(requestForm, { resource_type: row.resource_type, name: row.name, version: row.version, tags: [...(row.tags || [])], source, hf_repo_id, hf_revision, hf_token: "", ms_repo_id, ms_revision, ms_token: "" });
  requestVisible.value = true;
}
function editResource(row: SharedResource) {
  resourceTagEditorForm.id = row.id;
  resourceTagEditorForm.name = row.name;
  resourceTagEditorForm.version = row.version;
  resourceTagEditorForm.tags = [...(row.tags || [])];
  resourceTagEditorVisible.value = true;
}
const saving = ref(false);
async function saveResourceInfo() {
  saving.value = true;
  try {
    const result = await updateSharedResourceInfo(resourceTagEditorForm.id, {
      name: resourceTagEditorForm.name,
      version: resourceTagEditorForm.version,
      tags: resourceTagEditorForm.tags,
    });
    const target = resources.value.find((item) => item.id === result.id);
    if (target) Object.assign(target, result);
    resourceTagEditorVisible.value = false;
    ElMessage.success("资源信息已更新");
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "保存失败，请重试");
  } finally {
    saving.value = false;
  }
}
async function removeResource(row: SharedResource) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.name}」？此操作仅删除记录，不会删除磁盘上的文件。`, "删除共享资源", { type: "warning" });
    await deleteSharedResource(row.id); ElMessage.success("已删除"); await load();
  } catch (err) {
    if (err !== "cancel") ElMessage.error(err instanceof Error ? err.message : "删除失败");
  }
}

async function syncResourceToComputeNodes(row: SharedResource) {
  try {
    const result = await triggerResourceSyncAllNodes(row.id);
    ElMessage.success(`已提交 ${result.node_count} 个节点同步任务`);
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : "同步任务提交失败");
  }
}
async function openResourceBrowser(row: SharedResource) {
  resBrowseResource.value = row; resBrowsePath.value = ""; resBrowseVisible.value = true;
  await loadResourceScan();
  if (resBrowseScan.value?.status === "unknown") await refreshResourceScan();
}
async function loadResourceScan() {
  if (!resBrowseResource.value) return;
  resBrowseScan.value = await getSharedResourceFiles(resBrowseResource.value.id, resBrowsePath.value);
}
async function refreshResourceScan() {
  if (!resBrowseResource.value) return;
  await scanSharedResource(resBrowseResource.value.id, resBrowsePath.value);
  for (let i = 0; i < 20; i += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); await loadResourceScan(); if (resBrowseScan.value?.status !== "scanning") break; }
}
async function enterResourceDir(name: string) { resBrowsePath.value = [resBrowsePath.value, name].filter(Boolean).join("/"); await loadResourceScan(); if (resBrowseScan.value?.status === "unknown") await refreshResourceScan(); }
async function upResourceDir() { resBrowsePath.value = resBrowsePath.value.split("/").slice(0, -1).join("/"); await loadResourceScan(); }
async function previewResourceFile(row: UserDirectoryEntry) {
  if (!resBrowseResource.value || row.type !== "file") return;
  previewVisible.value = true;
  previewLoading.value = true;
  previewData.value = null;
  try {
    const relativePath = [resBrowsePath.value, row.name].filter(Boolean).join("/");
    previewData.value = await previewSharedResourceFile(resBrowseResource.value.id, relativePath);
  } catch (error) {
    previewVisible.value = false;
    ElMessage.error(error instanceof Error ? error.message : "文件预览失败");
  } finally {
    previewLoading.value = false;
  }
}
async function loadDirectory(preserveIfScanning = false) {
  if (!currentUserId.value) return;
  let liveResult: UserDirectoryScan | null = null;
  try {
    liveResult = await getUserDirectoryLive(currentUserId.value, currentPath.value);
  } catch (e) {
    // 即时 ls 失败，回退到缓存扫描
    console.warn("[StorageCenter] live-ls 失败，回退到缓存扫描:", e);
  }
  // live-ls 成功但目录为空且有错误信息（如目录不存在），回退到缓存/触发扫描
  if (liveResult && liveResult.entries.length === 0 && liveResult.error) {
    console.warn("[StorageCenter] live-ls 目录为空，error:", liveResult.error);
    liveResult = null;
  }
  if (liveResult) {
    directory.value = liveResult;
    if (!currentPath.value) homeDirectory.value = directory.value;
    return;
  }
  const result = await getUserDirectory(currentUserId.value, currentPath.value);
  if (preserveIfScanning && result.status === "scanning" && result.entries.length === 0 && (directory.value?.entries?.length ?? 0) > 0) {
    directory.value = { ...result, entries: directory.value!.entries };
  } else {
    directory.value = result;
  }
  if (!currentPath.value) homeDirectory.value = directory.value;
}
async function loadHomeUsage() {
  if (!currentUserId.value) return;
  let liveResult: UserDirectoryScan | null = null;
  try {
    liveResult = await getUserDirectoryLive(currentUserId.value, "");
  } catch (e) {
    console.warn("[StorageCenter] live-ls 失败，回退到缓存扫描:", e);
  }
  if (liveResult && liveResult.entries.length === 0 && liveResult.error) {
    console.warn("[StorageCenter] live-ls 目录为空，error:", liveResult.error);
    liveResult = null;
  }
  if (liveResult) {
    homeDirectory.value = liveResult;
    if (!currentPath.value) directory.value = liveResult;
    return;
  }
  const result = await getUserDirectory(currentUserId.value, "");
  homeDirectory.value = result;
  if (!currentPath.value) directory.value = result;
}
async function refreshDirectory() {
  if (!currentUserId.value) return;
  let liveResult: UserDirectoryScan | null = null;
  try {
    liveResult = await getUserDirectoryLive(currentUserId.value, currentPath.value);
  } catch (e) {
    console.warn("[StorageCenter] refresh live-ls 失败，回退到异步扫描:", e);
  }
  if (liveResult && liveResult.entries.length === 0 && liveResult.error) {
    console.warn("[StorageCenter] refresh live-ls 目录为空，error:", liveResult.error);
    liveResult = null;
  }
  if (liveResult) {
    directory.value = liveResult;
    if (!currentPath.value) homeDirectory.value = directory.value;
    return;
  }
  await scanUserDirectory(currentUserId.value, currentPath.value);
  for (let i = 0; i < 15; i += 1) { await new Promise((resolve) => setTimeout(resolve, 1000)); await loadDirectory(true); if (directory.value?.status !== "scanning") break; }
  if (currentPath.value) await loadHomeUsage();
}
async function enter(name: string) {
  dirLoading.value = true;
  try {
    currentPath.value = [currentPath.value, name].filter(Boolean).join("/");
    await loadDirectory();
    if (directory.value?.status === "unknown" || (directory.value?.status === "ready" && directory.value.entries.length === 0 && directory.value.error)) await refreshDirectory();
  } finally { dirLoading.value = false; }
}
async function up() {
  dirLoading.value = true;
  try {
    currentPath.value = currentPath.value.split("/").slice(0, -1).join("/");
    await loadDirectory();
  } finally { dirLoading.value = false; }
}
async function previewMyFile(row: UserDirectoryEntry) {
  if (!currentUserId.value || row.type !== "file") return;
  previewVisible.value = true;
  previewLoading.value = true;
  previewData.value = null;
  try {
    const relativePath = [currentPath.value, row.name].filter(Boolean).join("/");
    previewData.value = await previewUserFile(currentUserId.value, relativePath);
  } catch (error) {
    previewVisible.value = false;
    ElMessage.error(error instanceof Error ? error.message : "文件预览失败");
  } finally {
    previewLoading.value = false;
  }
}
async function deleteMyFile(row: UserBrowseEntry) {
  if (!currentUserId.value || row._virtual) return;
  const relativePath = [currentPath.value, row.name].filter(Boolean).join("/");
  const label = row.type === "directory" ? `目录「${row.name}」及其所有内容` : `文件「${row.name}」`;
  await ElMessageBox.confirm(`确认删除 ${label}？此操作不可恢复。`, "删除确认", {
    type: "warning",
    confirmButtonText: "删除",
    confirmButtonClass: "el-button--danger",
  });
  try {
    await deleteUserFile(currentUserId.value, relativePath);
    // 乐观更新：立即从列表移除已删除项，无需等待重新扫描
    if (directory.value?.entries) {
      directory.value = { ...directory.value, entries: directory.value.entries.filter(e => e.name !== row.name) };
    }
    ElMessage.success("已删除");
    refreshDirectory(); // 后台重新扫描，不阻塞 UI
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}
function pickFiles() {
  if (uploading.value) return;
  if (fileInputRef.value) fileInputRef.value.value = "";
  fileInputRef.value?.click();
}
function pickFolder() {
  if (uploading.value) return;
  if (folderInputRef.value) folderInputRef.value.value = "";
  folderInputRef.value?.click();
}
function uploadTopLevelEntries(paths: string[], files: File[]) {
  const grouped = new Map<string, { type: "file" | "directory"; size_bytes: number }>();
  paths.forEach((path, index) => {
    const parts = path.split("/").filter(Boolean);
    const name = parts[0] || files[index]?.name || "";
    if (!name) return;
    const type = parts.length > 1 ? "directory" : "file";
    const current = grouped.get(name);
    grouped.set(name, {
      type: current?.type === "directory" || type === "directory" ? "directory" : "file",
      size_bytes: (current?.size_bytes || 0) + (files[index]?.size || 0),
    });
  });
  return Array.from(grouped, ([name, item]) => ({
    name,
    type: item.type,
    size_bytes: item.size_bytes,
    mtime: Math.floor(Date.now() / 1000),
    mode: "",
  }));
}
async function handleUploadSelection(event: Event, mode: "files" | "folder") {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files || []);
  input.value = "";
  if (!currentUserId.value || selected.length === 0) return;
  const paths = selected.map((file) => {
    const maybeFolderFile = file as File & { webkitRelativePath?: string };
    return mode === "folder" && maybeFolderFile.webkitRelativePath ? maybeFolderFile.webkitRelativePath : file.name;
  });
  uploading.value = true;
  resetUploadProgress();
  try {
    const result = await uploadUserFiles(currentUserId.value, currentPath.value, selected, paths, (progress) => {
      uploadProgress.loaded = progress.loaded;
      uploadProgress.total = progress.total;
      uploadProgress.percent = progress.percent;
    });
    if (result.scan) {
      directory.value = result.scan;
      if (!currentPath.value) homeDirectory.value = result.scan;
      else if (homeDirectory.value) homeDirectory.value = { ...homeDirectory.value, size_bytes: homeDirectory.value.size_bytes + result.bytes };
    } else if (directory.value) {
      const optimisticEntries = uploadTopLevelEntries(paths, selected);
      const existing = new Map(directory.value.entries.map((entry) => [entry.name, entry]));
      optimisticEntries.forEach((entry) => existing.set(entry.name, entry));
      directory.value = {
        ...directory.value,
        status: "ready",
        entries: Array.from(existing.values()).sort((a, b) => {
          if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
          return a.name.localeCompare(b.name, "zh");
        }),
        file_count: directory.value.file_count + result.count,
        size_bytes: directory.value.size_bytes + result.bytes,
      };
      if (!currentPath.value) homeDirectory.value = directory.value;
      else if (homeDirectory.value) homeDirectory.value = { ...homeDirectory.value, size_bytes: homeDirectory.value.size_bytes + result.bytes };
    }
    ElMessage.success(`已上传 ${result.count} 个文件，共 ${bytes(result.bytes)}`);
    void refreshDirectory().catch(() => loadDirectory());
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "上传失败");
    await loadDirectory();
  } finally {
    uploading.value = false;
    window.setTimeout(resetUploadProgress, 800);
  }
}
async function download(row?: { name: string; type?: string }) {
  if (!currentUserId.value) return;
  const path = [currentPath.value, row?.name || ""].filter(Boolean).join("/");
  const isDirectory = !row || row.type === "directory";
  const key = path || "__root__";
  if (downloadingKeys.value.includes(key)) return;
  downloadingKeys.value = [...downloadingKeys.value, key];
  let notif: { close: () => void } | null = null;
  if (isDirectory) {
    notif = ElNotification({ title: "打包下载", message: "正在打包目录，请稍候...", type: "info", duration: 0, showClose: false });
  }
  try {
    const url = `/api/storage/users/${currentUserId.value}/download?relative_path=${encodeURIComponent(path)}`;
    const response = await fetch(url, { headers: { Authorization: `Bearer ${authToken.value}` } });
    if (!response.ok) {
      const text = await response.text();
      let message = `下载失败 (${response.status})`;
      if (text) {
        try {
          const error = JSON.parse(text);
          message = error?.detail || error?.error || message;
        } catch {
          message = text;
        }
      }
      ElMessage.error(message);
      return;
    }
    const blob = await response.blob();
    const fallbackName = isDirectory
      ? `${(row?.name || currentPath.value.split("/").filter(Boolean).slice(-1)[0] || policy.value?.username || authUser.value?.username || "home")}.tar.gz`
      : (row?.name || currentPath.value.split("/").filter(Boolean).slice(-1)[0] || "download");
    const filename = downloadFilenameFromHeader(response.headers.get("content-disposition"), fallbackName);
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "下载失败");
  } finally {
    notif?.close();
    downloadingKeys.value = downloadingKeys.value.filter(k => k !== key);
  }
}
function openRequest(type: SharedResource["resource_type"]) { requestForm.resource_type = type; requestVisible.value = true; }
watch(activeTab, (tab) => router.replace({ query: { ...route.query, tab } }));
watch(activeTab, (tab) => {
  if (tab === "workspace-volumes") {
    void loadWorkspaceVolumes().catch((error) => {
      ElMessage.error(error instanceof Error ? error.message : "节点用户数据卷加载失败");
    });
  }
  if (tab === "node-cache" && !cacheLoaded.value) {
    void loadCacheMatrix();
  }
});
watch(() => route.query, (query) => {
  if (typeof query.tab === "string") activeTab.value = query.tab;
});
watch(previewData, (data) => {
  if (previewBlobUrl.value) {
    URL.revokeObjectURL(previewBlobUrl.value);
    previewBlobUrl.value = "";
  }
  if (!data?.data || !data?.mime) return;
  const hex = data.data;
  const bytes = new Uint8Array(Math.floor(hex.length / 2));
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  previewBlobUrl.value = URL.createObjectURL(new Blob([bytes], { type: data.mime }));
});
onMounted(load);

// 在"我的文件"tab 下，每 30 秒自动触发重新扫描（不阻塞，静默刷新）
let _fileRefreshTimer: ReturnType<typeof setInterval> | null = null;
watch(activeTab, (tab) => {
  if (tab === "files") {
    if (!_fileRefreshTimer) {
      _fileRefreshTimer = setInterval(async () => {
        if (activeTab.value !== "files") return;
        try { await refreshDirectory(); } catch { /* 静默忽略，不打断用户操作 */ }
      }, 30000);
    }
  } else {
    if (_fileRefreshTimer) { clearInterval(_fileRefreshTimer); _fileRefreshTimer = null; }
  }
}, { immediate: true });

// 有下载任务进行时每 5 秒自动轮询（仅刷新资源列表，不触发全页刷新）
let _refreshTimer: ReturnType<typeof setInterval> | null = null;
watch(resources, (list) => {
  const active = list.some(r => r.request_status === 'downloading');
  if (active && !_refreshTimer) {
    _refreshTimer = setInterval(() => pollResources(), 5000);
  } else if (!active && _refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}, { immediate: true });
onUnmounted(() => {
  if (_refreshTimer) clearInterval(_refreshTimer);
  if (_fileRefreshTimer) { clearInterval(_fileRefreshTimer); _fileRefreshTimer = null; }
  if (previewBlobUrl.value) {
    URL.revokeObjectURL(previewBlobUrl.value);
    previewBlobUrl.value = "";
  }
});

function storageStatusLabel(status: string | undefined): string {
  const map: Record<string, string> = { ok: 'status.ready', warning: 'status.pending', missing: 'status.missing', error: 'status.failed', unknown: 'nodes.unknown' };
  const key = map[status ?? 'unknown'];
  return key ? t(key) : status ?? t("nodes.unknown");
}
function storageStatusType(status: string | undefined): string {
  if (status === 'ok') return 'success';
  if (status === 'error' || status === 'missing') return 'danger';
  if (status === 'warning') return 'warning';
  return 'info';
}
function checkStatusLabel(row: { request_status?: string; check_status?: string }): string {
  if (row.request_status === 'failed') return t("storage.downloadFailed");
  const map: Record<string, string> = { ok: 'status.ready', failed: 'status.failed', unknown: 'nodes.unknown', checking: 'status.verifying' };
  const key = map[row.check_status ?? ''];
  return key ? t(key) : row.check_status ?? '—';
}
function checkStatusType(row: { request_status?: string; check_status?: string }): string {
  if (row.check_status === 'ok') return 'success';
  if (row.check_status === 'failed' || row.request_status === 'failed') return 'danger';
  if (row.check_status === 'checking') return 'warning';
  return '';
}
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <div class="stats-grid storage-stats">
      <el-card shadow="never"><span>{{ t("storage.status") }}</span><div style="margin:8px 0"><el-tag :type="storageStatusType(storage?.status)" effect="light" size="large">{{ storageStatusLabel(storage?.status) }}</el-tag></div><small>{{ storage?.hostname || t("storage.noStorageNode") }}</small></el-card>
      <el-card shadow="never"><span>{{ t("storage.usage") }}</span><strong>{{ storage ? gbytes(storage.used_gb, storage.total_gb) : "-" }}</strong><small>{{ t("storage.usedTotal") }}</small></el-card>
      <el-card shadow="never"><span>{{ t("storage.datasets") }}</span><strong>{{ allDatasets.length }}</strong><small>{{ bytes(allDatasets.reduce((s, x) => s + x.size_bytes, 0)) }}</small></el-card>
      <el-card shadow="never"><span>{{ t("storage.models") }}</span><strong>{{ allModels.length }}</strong><small>{{ bytes(allModels.reduce((s, x) => s + x.size_bytes, 0)) }}</small></el-card>
      <el-card shadow="never"><span>{{ t("storage.publicResourceTotal") }}</span><strong>{{ bytes(totalResourceBytes) }}</strong><small>{{ t("storage.collections", { count: resources.length }) }}</small></el-card>
    </div>
    
    <div v-if="isAdmin" class="storage-card-header">
      <el-tooltip :content="t('storage.settings')" placement="top" :show-after="300">
        <el-button :icon="Setting" size="small" @click="openSettings">{{ t("storage.settings") }}</el-button>
      </el-tooltip>
    </div>

    <el-card shadow="never">
      
      <el-tabs v-model="activeTab">
        <el-tab-pane :label="t('storage.datasets')" name="datasets">
          <div class="table-toolbar res-toolbar">
            <el-input v-model="datasetSearch" :placeholder="t('storage.searchByName')" clearable style="width:180px" />
            <el-select v-model="datasetTagFilter" multiple filterable collapse-tags collapse-tags-tooltip :placeholder="t('storage.filterByTag')" style="width:220px">
              <el-option-group v-for="group in datasetFilterTagGroups" :key="group.label" :label="group.label">
                <el-option v-for="opt in group.options" :key="opt.value" :label="opt.label" :value="opt.value" />
              </el-option-group>
            </el-select>
            <span class="toolbar-spacer" />
            <el-button v-if="isAdmin" type="primary" :icon="DataLine" @click="openRequest('dataset')">{{ t("storage.datasetRequest") }}</el-button>
          </div>
          <el-table :data="datasets" stripe>
            <el-table-column prop="name" :label="t('storage.name')" min-width="200" show-overflow-tooltip/>
            <el-table-column prop="version" :label="t('storage.provider')" width="120"/>
            <el-table-column :label="t('storage.source')" width="130"><template #default="{row}"><el-tag size="small" effect="plain">{{ sourceLabel(row) }}</el-tag></template></el-table-column>
            <el-table-column :label="t('storage.tags')" min-width="200">
              <template #default="{row}">
                <div v-if="row.tags?.length" style="display:flex;flex-wrap:wrap;gap:6px">
                  <el-tag v-for="tag in row.tags.slice(0, 3)" :key="tag" size="small" effect="light">{{ tag }}</el-tag>
                  <el-tooltip v-if="row.tags.length > 3" :content="row.tags.join(' · ')" placement="top" :show-after="300">
                    <el-tag size="small" effect="plain">+{{ row.tags.length - 3 }}</el-tag>
                  </el-tooltip>
                </div>
                <span v-else style="color:var(--el-text-color-placeholder)">-</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.files')" width="100"><template #default="{row}">{{ row.file_count || '-' }}</template></el-table-column>
            <el-table-column :label="t('storage.size')" width="120"><template #default="{row}">{{ bytes(row.size_bytes) }}</template></el-table-column>
            <el-table-column :label="t('storage.state')" min-width="160">
              <template #default="{row}">
                <div v-if="row.request_status === 'downloading'" class="dl-progress">
                  <div class="dl-phase">{{ progressPhaseLabel(row) }}</div>
                  <el-progress v-if="hasDeterminateProgress(row)" :percentage="progressPercent(row)" :stroke-width="6" style="width:130px;margin-top:3px"/>
                  <div v-else class="dl-bar"><div class="dl-bar-inner" /></div>
                  <div v-if="row.download_progress?.current_file" class="dl-file">{{ row.download_progress.current_file }}</div>
                  <div class="dl-file">{{ progressDetail(row) }}</div>
                </div>
                <el-tooltip v-else-if="row.request_status === 'failed' && row.check_error" :content="row.check_error" placement="top" :show-after="300">
                  <el-tag type="danger" style="cursor:help">{{ t("storage.downloadFailed") }}</el-tag>
                </el-tooltip>
                <el-tag v-else :type="checkStatusType(row)">
                  {{ checkStatusLabel(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.actions')" width="320">
              <template #default="{row}">
                <el-tooltip :content="t('storage.viewFiles')" placement="top" :show-after="300">
                  <el-button size="small" :icon="FolderOpened" @click="openResourceBrowser(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.syncToComputeNodes')" placement="top" :show-after="300">
                  <el-button size="small" type="primary" :icon="RefreshRight" @click="syncResourceToComputeNodes(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('common.edit')" placement="top" :show-after="300">
                  <el-button size="small" :icon="Edit" @click="editResource(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin && row.request_status === 'failed' && row.source_url" :content="t('storage.retryDownload')" placement="top" :show-after="300">
                  <el-button size="small" type="warning" :icon="RefreshRight" @click="redownload(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.verify')" placement="top" :show-after="300">
                  <el-button size="small" :icon="CircleCheck" @click="checkResource(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.delete')" placement="top" :show-after="300">
                  <el-button size="small" type="danger" :icon="Delete" @click="removeResource(row)" />
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="t('storage.models')" name="models">
          <div class="table-toolbar res-toolbar">
            <el-input v-model="modelSearch" :placeholder="t('storage.searchByName')" clearable style="width:180px" />
            <el-select v-model="modelTagFilter" multiple filterable collapse-tags collapse-tags-tooltip :placeholder="t('storage.filterByTag')" style="width:220px">
              <el-option-group v-for="group in modelFilterTagGroups" :key="group.label" :label="group.label">
                <el-option v-for="opt in group.options" :key="opt.value" :label="opt.label" :value="opt.value" />
              </el-option-group>
            </el-select>
            <span class="toolbar-spacer" />
            <el-button v-if="isAdmin" type="primary" :icon="Connection" @click="openRequest('huggingface_model')">{{ t("storage.modelRequest") }}</el-button>
          </div>
          <el-table :data="models" stripe>
            <el-table-column prop="name" :label="t('storage.name')" min-width="200" show-overflow-tooltip/>
            <el-table-column prop="version" :label="t('storage.provider')" width="120"/>
            <el-table-column :label="t('storage.source')" width="130"><template #default="{row}"><el-tag size="small" effect="plain">{{ sourceLabel(row) }}</el-tag></template></el-table-column>
            <el-table-column :label="t('storage.tags')" min-width="200">
              <template #default="{row}">
                <div v-if="row.tags?.length" style="display:flex;flex-wrap:wrap;gap:6px">
                  <el-tag v-for="tag in row.tags.slice(0, 3)" :key="tag" size="small" effect="light">{{ tag }}</el-tag>
                  <el-tooltip v-if="row.tags.length > 3" :content="row.tags.join(' · ')" placement="top" :show-after="300">
                    <el-tag size="small" effect="plain">+{{ row.tags.length - 3 }}</el-tag>
                  </el-tooltip>
                </div>
                <span v-else style="color:var(--el-text-color-placeholder)">-</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.files')" width="100"><template #default="{row}">{{ row.file_count || '-' }}</template></el-table-column>
            <el-table-column :label="t('storage.size')" width="120"><template #default="{row}">{{ bytes(row.size_bytes) }}</template></el-table-column>
            <el-table-column :label="t('storage.state')" min-width="160">
              <template #default="{row}">
                <div v-if="row.request_status === 'downloading'" class="dl-progress">
                  <div class="dl-phase">{{ progressPhaseLabel(row) }}</div>
                  <el-progress v-if="hasDeterminateProgress(row)" :percentage="progressPercent(row)" :stroke-width="6" style="width:130px;margin-top:3px"/>
                  <div v-else class="dl-bar"><div class="dl-bar-inner" /></div>
                  <div v-if="row.download_progress?.current_file" class="dl-file">{{ row.download_progress.current_file }}</div>
                  <div class="dl-file">{{ progressDetail(row) }}</div>
                </div>
                <el-tooltip v-else-if="row.request_status === 'failed' && row.check_error" :content="row.check_error" placement="top" :show-after="300">
                  <el-tag type="danger" style="cursor:help">{{ t("storage.downloadFailed") }}</el-tag>
                </el-tooltip>
                <el-tag v-else :type="checkStatusType(row)">
                  {{ checkStatusLabel(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.actions')" width="320">
              <template #default="{row}">
                <el-tooltip :content="t('storage.viewFiles')" placement="top" :show-after="300">
                  <el-button size="small" :icon="FolderOpened" @click="openResourceBrowser(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.syncToComputeNodes')" placement="top" :show-after="300">
                  <el-button size="small" type="primary" :icon="RefreshRight" @click="syncResourceToComputeNodes(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('common.edit')" placement="top" :show-after="300">
                  <el-button size="small" :icon="Edit" @click="editResource(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin && row.request_status === 'failed' && row.source_url" :content="t('storage.retryDownload')" placement="top" :show-after="300">
                  <el-button size="small" type="warning" :icon="RefreshRight" @click="redownload(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.verify')" placement="top" :show-after="300">
                  <el-button size="small" :icon="CircleCheck" @click="checkResource(row)" />
                </el-tooltip>
                <el-tooltip v-if="isAdmin" :content="t('storage.delete')" placement="top" :show-after="300">
                  <el-button size="small" type="danger" :icon="Delete" @click="removeResource(row)" />
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="t('storage.myFiles')" name="files">
          <div class="card-header">
            <div class="my-files-head">
              <div>
                <strong>/{{ currentPath }}</strong>
                <small class="field-hint">{{ policy?.zfs_mountpoint || policy?.home_path }}</small>
              </div>
              <div class="quota-meter">
                <div class="quota-line">
                  <span>{{ t("storage.myFilesUsage") }}</span>
                  <strong>{{ homeUsageText }}</strong>
                </div>
                <el-progress
                  v-if="storageQuotaBytes"
                  :percentage="homeUsagePercent"
                  :stroke-width="6"
                  :show-text="false"
                  :status="homeUsagePercent >= 95 ? 'exception' : homeUsagePercent >= 80 ? 'warning' : undefined"
                />
              </div>
              <div v-if="uploading || uploadProgress.percent > 0" class="upload-meter">
                <div class="quota-line">
                  <span>{{ t("storage.uploadProgress") }}</span>
                  <strong>{{ uploadProgress.total ? `${bytes(uploadProgress.loaded)} / ${bytes(uploadProgress.total)}` : t("storage.preparingUpload") }}</strong>
                </div>
                <el-progress :percentage="uploadProgress.percent" :stroke-width="6" :show-text="false" />
              </div>
            </div>
            <div class="file-actions">
              <input ref="fileInputRef" class="hidden-file-input" type="file" multiple @change="handleUploadSelection($event, 'files')" />
              <input ref="folderInputRef" class="hidden-file-input" type="file" multiple webkitdirectory directory @change="handleUploadSelection($event, 'folder')" />
              <el-tooltip :content="t('storage.uploadFiles')" placement="top" :show-after="300">
                <el-button :icon="Upload" :loading="uploading" :disabled="!currentUserId" @click="pickFiles" />
              </el-tooltip>
              <el-tooltip :content="t('storage.uploadFolder')" placement="top" :show-after="300">
                <el-button :icon="FolderAdd" :loading="uploading" :disabled="!currentUserId" @click="pickFolder" />
              </el-tooltip>
              <el-tooltip :content="t('storage.refreshDirectory')" placement="top" :show-after="300">
                <el-button :icon="Refresh" :disabled="uploading" @click="refreshDirectory" />
              </el-tooltip>
            </div>
          </div>
          <el-table v-loading="dirLoading" :data="userBrowseEntriesPaged" stripe>
            <el-table-column :label="t('storage.name')">
              <template #default="{row}">
                <el-button v-if="row._virtual === 'parent'" link type="primary" @click="up">{{ t("storage.parent") }}</el-button>
                <el-button v-else-if="row.type === 'directory'" link type="primary" @click="enter(row.name)">{{ row.name }}/</el-button>
                <span v-else>{{ row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="type" :label="t('storage.type')" width="100"/>
            <el-table-column :label="t('storage.size')" width="120"><template #default="{row}">{{ bytes(row.size_bytes) }}</template></el-table-column>
            <el-table-column prop="mode" :label="t('storage.permissions')" width="130"/>
            <el-table-column :label="t('storage.actions')" width="200">
              <template #default="{row}">
                <el-tooltip v-if="!row._virtual && previewable(row)" :content="t('storage.preview')" placement="top" :show-after="300">
                  <el-button size="small" :icon="View" @click="previewMyFile(row)" />
                </el-tooltip>
                <el-tooltip v-if="!row._virtual" :content="t('storage.download')" placement="top" :show-after="300">
                  <el-button size="small" :icon="Download" :loading="isDownloading(row.name)" @click="download(row)" />
                </el-tooltip>
                <el-tooltip v-if="!row._virtual" :content="t('storage.delete')" placement="top" :show-after="300">
                  <el-button size="small" type="danger" :icon="Delete" @click="deleteMyFile(row)" />
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="userBrowseEntries.filter(e => !e._virtual).length > FILE_PAGE_SIZE"
            v-model:current-page="filePage"
            :page-size="FILE_PAGE_SIZE"
            :total="userBrowseEntries.filter(e => !e._virtual).length"
            layout="total, prev, pager, next"
            style="margin-top: 12px; justify-content: flex-end"
          />
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" :label="t('storage.zfsUsers')" name="zfs-users">
          <div class="card-header" style="margin-bottom:12px">
            <div>
              <strong>{{ t("storage.userDataset") }}</strong>
              <small class="field-hint">{{ t("storage.userDatasetHint") }}</small>
            </div>
            <div class="file-actions">
              <el-tooltip :content="t('storage.refreshStatus')" placement="top" :show-after="300">
                <el-button :icon="Refresh" @click="loadUserDatasets" />
              </el-tooltip>
              <el-tooltip :content="t('storage.ensureAll')" placement="top" :show-after="300">
                <el-button type="primary" :icon="CircleCheck" :loading="zfsEnsuring" @click="ensureAllZfsDatasets" />
              </el-tooltip>
            </div>
          </div>
          <el-table :data="userDatasets" stripe>
            <el-table-column prop="username" :label="t('storage.user')" width="140" />
            <el-table-column :label="t('storage.enabled')" width="80">
              <template #default="{row}"><el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? t("storage.yes") : t("storage.no") }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="home_path" :label="t('storage.platformPath')" min-width="190" show-overflow-tooltip />
            <el-table-column prop="mountpoint" :label="t('storage.mountpoint')" min-width="220" show-overflow-tooltip />
            <el-table-column prop="dataset_name" label="Dataset" min-width="200" show-overflow-tooltip />
            <el-table-column label="Quota" width="100">
              <template #default="{row}">{{ row.quota_gb || row.storage_quota_gb || 0 ? `${row.quota_gb || row.storage_quota_gb} GB` : t("storage.unlimited") }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.node')" width="150">
              <template #default="{row}">{{ row.node || '-' }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.state')" width="130">
              <template #default="{row}">
                <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top" :show-after="300">
                  <el-tag :type="zfsStatusType(row.status)" style="cursor:help">{{ row.status || 'unknown' }}</el-tag>
                </el-tooltip>
                <el-tag v-else :type="zfsStatusType(row.status)">{{ row.status || 'unknown' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.appliedAt')" width="180">
              <template #default="{row}">{{ formatTime(row.applied_at) }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.actions')" width="130" fixed="right">
              <template #default="{row}">
                <div style="display:flex;gap:8px">
                  <el-tooltip :content="t('storage.ensureDataset')" placement="top" :show-after="300">
                    <el-button size="small" :icon="CircleCheck" :loading="zfsBusy(row)" @click="ensureZfsDataset(row)" />
                  </el-tooltip>
                  <el-tooltip :content="row.enabled ? '只能删除未启用用户的 Dataset' : row.status === 'removing' ? '重新提交删除任务' : '删除存储节点上的用户 Dataset'" placement="top" :show-after="300">
                    <el-button
                      size="small"
                      type="danger"
                      :icon="Delete"
                      :loading="zfsBusy(row)"
                      :disabled="row.enabled || row.status === 'pending'"
                      @click="removeZfsDataset(row)"
                    />
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" :label="t('storage.workspaceVolumes')" name="workspace-volumes">
          <div class="card-header" style="margin-bottom:12px">
            <div>
              <strong>{{ t("storage.workspaceVolumesTitle") }}</strong>
              <small class="field-hint">{{ t("storage.workspaceVolumesHint") }}</small>
            </div>
            <el-tooltip :content="t('storage.refreshStatus')" placement="top" :show-after="300">
              <el-button :icon="Refresh" :loading="workspaceLoading" @click="refreshWorkspaceVolumesDiskUsage" />
            </el-tooltip>
          </div>
          <el-table :data="workspaceVolumes" stripe>
            <el-table-column prop="node" :label="t('storage.node')" width="150" />
            <el-table-column prop="username" :label="t('storage.user')" width="140" />
            <el-table-column prop="volume_name" :label="t('storage.volume')" min-width="170" show-overflow-tooltip />
            <el-table-column :label="t('storage.quotaLimit')" width="110">
              <template #default="{row}">{{ row.quota_gb ? `${row.quota_gb} GB` : t("storage.unlimited") }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.used')" width="110">
              <template #default="{row}">{{ row.used_gb == null ? '–' : gbValue(row.used_gb) }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.containers')" width="120">
              <template #default="{row}">{{ row.active_container_count }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.nodeStatus')" width="110">
              <template #default="{row}"><el-tag :type="row.node_status === 'online' ? 'success' : 'info'" size="small">{{ row.node_status }}</el-tag></template>
            </el-table-column>
            <el-table-column :label="t('storage.volumeStatus')" width="130">
              <template #default="{row}">
                <el-tooltip v-if="row.last_error" :content="row.last_error" placement="top" :show-after="300">
                  <el-tag :type="workspaceStatusType(row.status)" style="cursor:help">{{ row.status }}</el-tag>
                </el-tooltip>
                <el-tag v-else :type="workspaceStatusType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.createdAt')" width="180">
              <template #default="{row}">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.actions')" width="110" fixed="right">
              <template #default="{row}">
                <el-tooltip :content="t('storage.reclaimVolume')" placement="top" :show-after="300">
                  <el-button
                    size="small"
                    type="danger"
                    :icon="Delete"
                    :loading="workspaceBusy(row)"
                    :disabled="row.status === 'removed' || row.status === 'removing' || row.active_container_count > 0 || row.node_status !== 'online'"
                    @click="removeWorkspaceVolumeAction(row)"
                  />
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" :label="t('storage.nodeCache')" name="node-cache">
          <div class="card-header" style="margin-bottom:12px">
            <div>
              <strong>{{ t("storage.nodeCacheTitle") }}</strong>
              <small class="field-hint">{{ t("storage.nodeCacheHint") }}</small>
            </div>
            <el-tooltip :content="t('storage.refreshStatus')" placement="top" :show-after="300">
              <el-button :icon="Refresh" :loading="cacheLoading" @click="loadCacheMatrix" />
            </el-tooltip>
          </div>
          <div class="table-toolbar res-toolbar" style="margin-bottom:12px">
            <el-select v-model="cacheStatusFilter" :placeholder="t('storage.filterByStatus')" clearable style="width:160px">
              <el-option value="ready" :label="t('storage.cacheStatus') + ': ready'" />
              <el-option value="syncing" :label="t('storage.cacheStatus') + ': syncing'" />
              <el-option value="pending" :label="t('storage.cacheStatus') + ': pending'" />
              <el-option value="failed" :label="t('storage.cacheStatus') + ': failed'" />
            </el-select>
            <el-select v-model="cacheNodeFilter" :placeholder="t('storage.filterByNode')" clearable style="width:180px">
              <el-option v-for="h in cacheNodeOptions" :key="h" :label="h" :value="h" />
            </el-select>
            <span class="toolbar-spacer" />
            <span style="color:var(--muted);font-size:12px">{{ filteredCacheMatrix.length }} / {{ cacheMatrix.length }} 条记录</span>
          </div>
          <el-table v-loading="cacheLoading" :data="filteredCacheMatrix" stripe>
            <el-table-column :label="t('storage.node')" prop="hostname" width="160" show-overflow-tooltip />
            <el-table-column :label="t('storage.resource')" min-width="200" show-overflow-tooltip>
              <template #default="{row}">{{ row.resource_name }}<small style="color:var(--muted);margin-left:6px">{{ row.version }}</small></template>
            </el-table-column>
            <el-table-column :label="t('storage.source')" width="120">
              <template #default="{row}"><el-tag size="small" effect="plain">{{ row.resource_type === 'dataset' ? t('storage.datasets').replace('公开','').trim() : 'Model' }}</el-tag></template>
            </el-table-column>
            <el-table-column :label="t('storage.cacheStatus')" width="120">
              <template #default="{row}">
                <el-tooltip v-if="row.status === 'failed' && row.error" :content="row.error" placement="top" :show-after="300">
                  <el-tag :type="cacheStatusType(row.status)" size="small" style="cursor:help">{{ row.status }}</el-tag>
                </el-tooltip>
                <el-tag v-else :type="cacheStatusType(row.status)" size="small">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('storage.localPath')" min-width="220" show-overflow-tooltip>
              <template #default="{row}"><span style="font-size:12px;color:var(--muted)">{{ row.local_path || '-' }}</span></template>
            </el-table-column>
            <el-table-column :label="t('storage.cachedAt')" width="180">
              <template #default="{row}">{{ formatTime(row.synced_at ?? 0) }}</template>
            </el-table-column>
            <el-table-column :label="t('storage.actions')" width="130" fixed="right">
              <template #default="{row}">
                <div style="display:flex;gap:8px">
                  <el-tooltip :content="t('storage.triggerSync')" placement="top" :show-after="300">
                    <el-button
                      size="small"
                      :icon="Refresh"
                      :loading="cacheBusy(row)"
                      :disabled="row.status === 'syncing'"
                      @click="triggerCacheSync(row)"
                    />
                  </el-tooltip>
                  <el-tooltip :content="t('storage.clearCache')" placement="top" :show-after="300">
                    <el-button
                      size="small"
                      type="danger"
                      :icon="Delete"
                      :loading="cacheBusy(row)"
                      :disabled="row.status === 'syncing'"
                      @click="clearCacheRecord(row)"
                    />
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
      
    </el-card>

    <!-- 下载请求对话框 -->
    <el-dialog v-model="requestVisible" :title="requestForm.resource_type === 'dataset' ? '下载数据集到共享存储' : '下载模型到共享存储'" width="640px">
      <el-form :model="requestForm" label-position="top">
        <el-form-item label="本地集合名称"><el-input v-model="requestForm.name" placeholder="小写字母/数字/连字符，用于本地目录命名"/></el-form-item>
        <el-form-item label="提供者"><el-input v-model="requestForm.version" placeholder="例如 openmoss / openai / qwen（默认 default）"/></el-form-item>
        <el-form-item label="标签">
          <el-select v-model="requestForm.tags" multiple filterable allow-create collapse-tags collapse-tags-tooltip style="width:100%" placeholder="选择或输入标签">
            <el-option-group v-for="group in resourceTagGroups" :key="group.label" :label="group.label">
              <el-option v-for="opt in group.options" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="下载源">
          <el-radio-group v-model="requestForm.source">
            <el-radio value="modelscope">ModelScope（推荐，国内可直连）</el-radio>
            <el-radio value="huggingface">HuggingFace（可用镜像或代理）</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="requestForm.source === 'modelscope'">
          <el-alert type="success" :closable="false" title="ModelScope（魔搭）在国内可直连，无需代理。仓库 ID 格式同 HuggingFace，如 AI-ModelScope/bert-base-uncased。" style="margin-bottom:12px"/>
          <el-form-item label="ModelScope 仓库 ID"><el-input v-model="requestForm.ms_repo_id" placeholder="例如 AI-ModelScope/bert-base-uncased"/></el-form-item>
          <el-form-item label="Revision（分支/Tag）"><el-input v-model="requestForm.ms_revision" placeholder="默认 master"/></el-form-item>
          <el-form-item label="MS Token（可选）"><el-input v-model="requestForm.ms_token" type="password" show-password placeholder="可选"/></el-form-item>
        </template>
        <template v-else>
          <el-alert type="warning" :closable="false" title="国内访问 HuggingFace 可在 .env 中配置 HF_ENDPOINT=https://hf-mirror.com，或配置有效的 HTTPS_PROXY 代理。" style="margin-bottom:12px"/>
          <el-form-item label="HuggingFace 仓库 ID"><el-input v-model="requestForm.hf_repo_id" placeholder="例如 meta-llama/Llama-3.2-1B"/></el-form-item>
          <el-form-item label="Revision（分支/Tag）"><el-input v-model="requestForm.hf_revision" placeholder="默认 main"/></el-form-item>
          <el-form-item label="HF Token（可选，访问 gated 模型）"><el-input v-model="requestForm.hf_token" type="password" show-password placeholder="hf_xxxxx"/></el-form-item>
        </template>
      </el-form>
      <template #footer><el-button :icon="Close" @click="requestVisible=false">取消</el-button><el-button type="primary" :icon="Download" :loading="requesting" @click="submitRequest">提交下载</el-button></template>
    </el-dialog>

    <el-dialog v-model="resourceTagEditorVisible" title="编辑资源" width="560px">
      <el-form :model="resourceTagEditorForm" label-position="top">
        <el-form-item label="名称">
          <el-input v-model="resourceTagEditorForm.name" placeholder="小写字母/数字/连字符" />
        </el-form-item>
        <el-form-item label="提供者">
          <el-input v-model="resourceTagEditorForm.version" placeholder="例如 openmoss / openai / qwen（默认 default）" />
        </el-form-item>
        <el-form-item label="标签">
          <el-select v-model="resourceTagEditorForm.tags" multiple filterable allow-create collapse-tags collapse-tags-tooltip style="width:100%" placeholder="选择或输入标签">
            <el-option-group v-for="group in resourceTagGroups" :key="group.label" :label="group.label">
              <el-option v-for="opt in group.options" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-option-group>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="resourceTagEditorVisible=false">取消</el-button>
        <el-button type="primary" :icon="Select" :loading="saving" @click="saveResourceInfo">保存</el-button>
      </template>
    </el-dialog>

    <!-- 资源文件浏览器 -->
    <el-dialog v-model="resBrowseVisible" :title="`文件浏览 — ${resBrowseResource?.name || ''}${resBrowsePath ? ' / ' + resBrowsePath : ''}`" width="780px">
      <div class="card-header res-browse-header">
        <div>
          <strong>{{ resBrowseScan?.file_count ?? '-' }} 个文件</strong>
          <small class="field-hint">共 {{ bytes(resBrowseScan?.size_bytes ?? 0) }}</small>
        </div>
        <div>
          <el-button :icon="Refresh" @click="refreshResourceScan">刷新</el-button>
        </div>
      </div>
      <el-table :data="resourceBrowseEntries" stripe style="margin-top:8px">
        <el-table-column label="名称">
          <template #default="{row}">
            <el-button v-if="row._virtual === 'parent'" link type="primary" @click="upResourceDir">../ 上一级</el-button>
            <el-button v-else-if="row.type === 'directory'" link type="primary" @click="enterResourceDir(row.name)">{{ row.name }}/</el-button>
            <span v-else>{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="100"/>
        <el-table-column label="大小" width="120"><template #default="{row}">{{ bytes(row.size_bytes) }}</template></el-table-column>
        <el-table-column prop="mode" label="权限" width="130"/>
        <el-table-column label="操作" width="100">
          <template #default="{row}">
            <el-button v-if="!row._virtual && previewable(row)" size="small" :icon="View" @click="previewResourceFile(row)">预览</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button :icon="Close" @click="resBrowseVisible=false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="previewVisible" :title="`文件预览 — ${previewData?.name || ''}`" width="860px">
      <div v-loading="previewLoading" class="preview-wrap">
        <template v-if="previewData && !previewLoading">
          <div class="preview-meta">{{ bytes(previewData.size_bytes) }}<span v-if="previewData.mime"> · {{ previewData.mime }}</span></div>
          <pre v-if="previewData.kind === 'text'" class="preview-text">{{ previewData.text }}</pre>
          <img v-else-if="previewData.kind === 'image'" :src="previewDataUrl()" class="preview-image" />
          <video v-else-if="previewData.kind === 'video'" :src="previewDataUrl()" class="preview-video" controls preload="metadata" />
          <iframe v-else-if="previewData.kind === 'pdf'" :src="previewDataUrl()" class="preview-pdf" />
          <el-empty v-else :description="previewData.message || '当前文件不支持预览'" />
        </template>
      </div>
      <template #footer><el-button :icon="Close" @click="previewVisible=false">关闭</el-button></template>
    </el-dialog>

    <!-- 存储设置 -->
    <el-dialog v-model="settingsVisible" title="存储设置" width="540px">
      <el-alert type="info" :closable="false" style="margin-bottom:16px"
        title="修改后仅对后续新增操作生效，已有资源路径不受影响。"/>
      <el-form :model="settingsForm" label-position="top">
        <el-form-item label="数据集存储基路径">
          <el-input v-model="settingsForm.dataset_base_path" placeholder="/data/datasets"/>
          <div class="field-hint">数据集将保存到 {基路径}/{名称}/{提供者}</div>
        </el-form-item>
        <el-form-item label="模型存储基路径">
          <el-input v-model="settingsForm.model_base_path" placeholder="/data/models/huggingface"/>
          <div class="field-hint">模型将保存到 {基路径}/{名称}/{提供者}</div>
        </el-form-item>
        <el-form-item label="我的文件存储基路径">
          <el-input v-model="settingsForm.user_base_path" placeholder="/data/users"/>
          <div class="field-hint">新用户的主目录将创建在 {基路径}/{用户名}</div>
        </el-form-item>
        <el-form-item label="HuggingFace 下载端点">
          <el-switch
            v-model="settingsForm.hf_endpoint_enabled"
            active-text="启用自定义 HF_ENDPOINT"
            inactive-text="使用服务环境默认值"
          />
          <el-input
            v-model="settingsForm.hf_endpoint"
            :disabled="!settingsForm.hf_endpoint_enabled"
            placeholder="https://hf-mirror.com"
            style="margin-top:8px"
          />
          <div class="field-hint">国内访问可填写 https://hf-mirror.com；关闭时使用后端容器环境中的 HF_ENDPOINT。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="settingsVisible=false">取消</el-button>
        <el-button type="primary" :icon="Select" :loading="settingsSaving" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.storage-card-header{display:flex;justify-content:flex-end;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
.res-toolbar{justify-content:flex-start;gap:8px}
.toolbar-spacer{flex:1}
.storage-stats{grid-template-columns:repeat(5,minmax(150px,1fr))}
.storage-stats span:not(.el-tag),.storage-stats small{display:block;color:var(--muted);font-size:12px}
.storage-stats strong{display:block;margin:8px 0;font-size:24px}
.task-toolbar{margin-bottom:12px}
.restore-title{margin-top:24px}
.task-filters{display:grid;grid-template-columns:minmax(260px,1fr) 180px auto;gap:10px;margin-bottom:12px}
.res-browse-header{margin-bottom:4px}
.my-files-head{display:flex;align-items:center;gap:18px;min-width:0}
.quota-meter,.upload-meter{width:260px;max-width:42vw}
.quota-line{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px;color:var(--muted);font-size:12px}
.quota-line strong{color:var(--el-text-color-primary);font-size:12px;font-weight:600;white-space:nowrap}
.file-actions{display:flex;align-items:center;gap:8px}
.hidden-file-input{display:none}
.field-hint{display:block;color:var(--muted);font-size:12px;margin-top:4px}
.dl-progress{line-height:1.5}
.dl-phase{font-size:12px;color:#409eff;font-weight:500}
.dl-file{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.preview-wrap{min-height:180px}
.preview-meta{margin-bottom:10px;color:var(--muted);font-size:12px}
.preview-text{max-height:60vh;overflow:auto;margin:0;padding:14px;border-radius:10px;background:#0f172a;color:#e2e8f0;font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.preview-image,.preview-video{display:block;max-width:100%;max-height:60vh;margin:0 auto;border-radius:10px;background:#000}
.preview-pdf{display:block;width:100%;height:70vh;border:0;border-radius:10px;background:#f5f7fa}
.dl-bar{position:relative;overflow:hidden;width:130px;height:6px;margin-top:3px;border-radius:999px;background:#e4e7ed}
.dl-bar-inner{position:absolute;inset:0 auto 0 -45%;width:45%;border-radius:999px;background:linear-gradient(90deg,#79bbff,#409eff);animation:dl-slide 1.2s ease-in-out infinite}
@keyframes dl-slide{0%{left:-45%}100%{left:100%}}
@media (max-width: 720px){
  .my-files-head{align-items:flex-start;flex-direction:column;gap:8px}
  .quota-meter,.upload-meter{width:min(100%,320px);max-width:100%}
}
</style>
