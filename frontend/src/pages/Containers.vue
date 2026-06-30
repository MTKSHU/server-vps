<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  CircleClose,
  Close,
  Connection,
  Delete,
  Download,
  FolderOpened,
  Plus,
  Position,
  Refresh,
  RefreshRight,
  Select,
  Setting,
  Switch,
  Tickets,
  TurnOff,
  Upload,
  VideoPlay,
} from "@element-plus/icons-vue";
import StatusTag from "../components/StatusTag.vue";
import CreateContainer from "./CreateContainer.vue";
import DirectoryPicker from "../components/DirectoryPicker.vue";
import {
  containerAction,
  retryContainer,
  retryContainerSshAccess,
  createContainerPort,
  deleteContainer,
  deleteContainerPort,
  deleteContainerSyncRule,
  getContainers,
  getContainerSyncRules,
  getContainerSyncTasks,
  getSharedResources,
  runContainerSync,
  runContainerSyncRule,
  saveContainerSyncRule,
  updateContainerPort,
  updateContainerResources,
  publishContainerImage,
  getUserPreference,
  updateUserPreference,
  type Container,
  type ContainerPort,
  type ContainerSyncRule,
  type DataSyncTask,
  type SharedResource,
} from "../api/cluster";
import { authUser, hasAdminAccess } from "../auth";

const router = useRouter();
const loading = ref(false);
const isAdmin = computed(() => hasAdminAccess());
const containers = ref<Container[]>([]);
const createDialogVisible = ref(false);
const portDialogVisible = ref(false);
const imageDialogVisible = ref(false);
const imagePublishing = ref(false);
const selectedContainer = ref<Container | null>(null);
const editingPort = ref<ContainerPort | null>(null);
const syncDialogVisible = ref(false);
const syncLoading = ref(false);
const syncSubmitting = ref(false);
const syncRules = ref<ContainerSyncRule[]>([]);
const syncTasks = ref<DataSyncTask[]>([]);
const sharedResources = ref<SharedResource[]>([]);

// ── 列设置 ──────────────────────────────
const columnPreferenceKey = "containers.visible_columns";
const columnOptions = computed(() => {
  const options = [
    { key: "image_name", label: "镜像", defaultVisible: true },
    { key: "spec", label: "规格", defaultVisible: true },
    { key: "gpu", label: "GPU", defaultVisible: true },
    { key: "status", label: "状态", defaultVisible: true },
    { key: "ports", label: "端口", defaultVisible: true },
    { key: "connection", label: "连接", defaultVisible: true },
  ];
  if (isAdmin.value) {
    options.unshift({ key: "owner", label: "创建者", defaultVisible: true });
  }
  return options;
});
const defaultVisibleColumns = computed(() =>
  columnOptions.value.filter((c) => c.defaultVisible).map((c) => c.key)
);
const visibleColumns = ref<string[]>([...defaultVisibleColumns.value]);

const visibleColumnDefs = computed(() => {
  const byKey = new Map(columnOptions.value.map((c) => [c.key, c]));
  return visibleColumns.value.map((key) => byKey.get(key)).filter(Boolean) as typeof columnOptions.value;
});

function sanitizeColumns(value: unknown) {
  const validColumns = new Set(columnOptions.value.map((c) => c.key));
  if (Array.isArray(value)) {
    return value.filter((key): key is string => typeof key === "string" && validColumns.has(key));
  }
  return [];
}

function orderedColumns(keys: string[]) {
  const selected = new Set(keys);
  return [
    ...keys.filter((key) => columnOptions.value.some((c) => c.key === key)),
    ...columnOptions.value.filter((c) => !selected.has(c.key)).map((c) => c.key),
  ];
}

async function loadVisibleColumns() {
  try {
    const pref = await getUserPreference<{ columns?: unknown[] }>(columnPreferenceKey);
    const stored = sanitizeColumns(pref.value.columns);
    visibleColumns.value = stored.length ? stored : [...defaultVisibleColumns.value];
  } catch {
    visibleColumns.value = [...defaultVisibleColumns.value];
  }
}

async function saveVisibleColumns() {
  try {
    await updateUserPreference(columnPreferenceKey, { columns: visibleColumns.value });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "列设置保存失败");
  }
}

function resetVisibleColumns() {
  visibleColumns.value = [...defaultVisibleColumns.value];
  saveVisibleColumns();
}

function toggleColumn(key: string, checked: boolean) {
  if (checked) {
    visibleColumns.value = orderedColumns([...visibleColumns.value, key]);
  } else {
    const next = visibleColumns.value.filter((item) => item !== key);
    visibleColumns.value = next.length ? next : ["status"];
  }
  saveVisibleColumns();
}

function moveColumn(index: number, direction: -1 | 1) {
  const target = index + direction;
  if (target < 0 || target >= visibleColumns.value.length) return;
  const next = [...visibleColumns.value];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  visibleColumns.value = next;
  saveVisibleColumns();
}

type PortProtocol = "tcp" | "udp" | "both";
type PortType = "ssh" | "web" | "custom";
const portForm = reactive({ name: "ssh", port_type: "ssh" as PortType, protocol: "tcp" as PortProtocol, container_port: 22 });
const syncDownloadForm = reactive({
  storage_type: "user_file" as "dataset" | "model" | "user_file",
  resource_id: undefined as number | undefined,
  storage_relative_path: "",
  container_path: "/workspace",
  conflict_policy: "overwrite" as "overwrite" | "skip",
});
const syncUploadForm = reactive({
  container_path: "/workspace",
  storage_relative_path: "",
  conflict_policy: "overwrite" as "overwrite" | "skip",
});
const syncRuleForm = reactive({
  name: "定时上传",
  container_path: "/workspace",
  storage_relative_path: "",
  schedule_kind: "daily" as "daily" | "weekly" | "monthly",
  hour: 0,
  minute: 0,
  second: 0,
  enabled: true,
  conflict_policy: "overwrite" as "overwrite" | "skip",
});
const imageForm = reactive({
  alias: "",
  display_name: "",
  register_platform: true,
  export_to_storage: true,
});

// 目录选择器引用
const storageDirPicker = ref<InstanceType<typeof DirectoryPicker> | null>(null);
const pickerTarget = ref<"download_storage" | "upload_storage" | "rule_storage">("download_storage");

function openStorageDirPicker(target: typeof pickerTarget.value) {
  pickerTarget.value = target;
  storageDirPicker.value?.open();
}

function onStorageDirPicked(path: string) {
  const relPath = path || "";
  switch (pickerTarget.value) {
    case "download_storage":
      syncDownloadForm.storage_relative_path = relPath;
      break;
    case "upload_storage":
      syncUploadForm.storage_relative_path = relPath;
      break;
    case "rule_storage":
      syncRuleForm.storage_relative_path = relPath;
      break;
  }
}
const resourceDialogVisible = ref(false);
const resourceSaving = ref(false);
const resourceForm = reactive({ cpu_cores: 1, memory_gb: 1, gpu_count: 0, gpu_model: "" });
let refreshTimer: number | undefined;

async function load(showLoading = true) {
  if (showLoading) loading.value = true;
  try {
    containers.value = await getContainers();
  } finally {
    if (showLoading) loading.value = false;
  }
}

async function refreshQuietly() {
  if (document.hidden) return;
  await load(false);
}

async function action(id: number, next: "start" | "stop" | "restart") {
  await containerAction(id, next);
  ElMessage.success("操作任务已提交");
  await load();
}

async function retry(id: number) {
  await retryContainer(id);
  ElMessage.success("重试任务已提交");
  await load();
}

async function retrySshAccess(id: number) {
  await retryContainerSshAccess(id);
  ElMessage.success("SSH 准备重试任务已提交");
  await load();
}

async function remove(row: Container) {
  const force = lifecycleBusy(row);
  const result = await ElMessageBox.prompt(
    force
      ? `容器 ${row.name} 处于 ${row.status} 状态，无法确认节点侧是否已创建 Incus 容器。此操作只会强制移除平台记录和关联任务，不会连接节点清理 Incus。请输入容器名称确认移除。`
      : `此操作会真实删除计算节点上的 Incus 容器和平台记录。请输入容器名称 ${row.name} 确认删除。`,
    force ? "移除容器记录" : "删除容器",
    {
      type: "warning",
      confirmButtonText: force ? "移除" : "删除",
      cancelButtonText: "取消",
      inputPattern: new RegExp(`^${row.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`),
      inputErrorMessage: "请输入完整容器名称"
    }
  );
  await deleteContainer(row.id, result.value, force);
  ElMessage.success(force ? "容器记录已移除" : "删除任务已提交");
  await load();
}

function gpuText(row: Container) {
  return row.gpus.length ? row.gpus.map((gpu) => `${gpu.model} #${gpu.slot}`).join(", ") : "无";
}

function openShell(row: Container) {
  router.push({ name: "containerShell", params: { id: row.id } });
}
function openResourceDialog(row: Container) {
  selectedContainer.value = row;
  resourceForm.cpu_cores = row.cpu_cores;
  resourceForm.memory_gb = row.memory_gb;
  resourceForm.gpu_count = row.gpus.length;
  resourceForm.gpu_model = row.gpus[0]?.model || "";
  resourceDialogVisible.value = true;
}

async function submitResourceUpdate() {
  if (!selectedContainer.value) return;
  resourceSaving.value = true;
  try {
    await updateContainerResources(selectedContainer.value.id, {
      cpu_cores: resourceForm.cpu_cores,
      memory_gb: resourceForm.memory_gb,
      gpu_count: resourceForm.gpu_count,
      gpu_model: resourceForm.gpu_model,
    });
    resourceDialogVisible.value = false;
    ElMessage.success("配置修改任务已提交，容器配置将即时生效");
    await load();
  } finally {
    resourceSaving.value = false;
  }
}

async function publishImage(row: Container) {
  selectedContainer.value = row;
  const base = row.name.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
  imageForm.alias = `${base || "container"}-image`;
  imageForm.display_name = `${row.name} 镜像`;
  imageForm.register_platform = true;
  imageForm.export_to_storage = true;
  imageDialogVisible.value = true;
}

async function submitPublishImage() {
  if (!selectedContainer.value) return;
  imagePublishing.value = true;
  try {
    await publishContainerImage(selectedContainer.value.id, {
      alias: imageForm.alias,
      display_name: imageForm.display_name,
      register_platform: imageForm.register_platform,
      export_to_storage: imageForm.export_to_storage,
    });
    imageDialogVisible.value = false;
    ElMessage.success("镜像上传任务已提交；完成后会显示在镜像管理的自建镜像中");
  } finally {
    imagePublishing.value = false;
  }
}

function publicHost() {
  return window.location.hostname || "管理节点IP";
}

function sshPort(row: Container) {
  return row.ports.find((port) => port.protocol === "tcp" && port.container_port === 22);
}

function publicPort(port: ContainerPort) {
  return port.public_port || port.host_port;
}

function nodeListenPort(port: ContainerPort) {
  return port.node_listen_port || port.node_port || 0;
}

function nodeHost(row: Container) {
  return row.node_ip || row.node || "节点IP";
}

function portsText(row: Container) {
  if (!row.ports || row.ports.length === 0) return "-";
  return row.ports
    .map((p) => `${publicPort(p)}:${p.container_port}/${p.protocol}`)
    .join(", ");
}

function nodePortsText(row: Container) {
  if (!row.ports || row.ports.length === 0) return "-";
  return row.ports
    .map((p) => {
      const listenPort = nodeListenPort(p);
      return listenPort ? `${listenPort}:${p.container_port}/${p.protocol}` : "";
    })
    .filter(Boolean)
    .join(", ") || "-";
}

function nodeSshCommand(row: Container, port: ContainerPort) {
  const listenPort = nodeListenPort(port);
  return listenPort ? `ssh ${row.ssh_username}@${nodeHost(row)} -p ${listenPort}` : "";
}

function lifecycleBusy(row: Container) {
  return ["provisioning", "starting", "stopping", "restarting", "deleting"].includes(row.status);
}

function accessLabel(row: Container) {
  if (row.status !== "running") return "";
  const map: Record<string, string> = {
    pending: "SSH 初始化",
    ready: "SSH 就绪",
    failed: "SSH 失败",
  };
  return map[row.access_status] || row.access_status || "";
}

function accessReady(row: Container) {
  return row.status === "running" && row.access_status === "ready";
}

function formatTime(timestamp: number) {
  if (!timestamp) return "-";
  return new Date(timestamp * 1000).toLocaleString();
}

function conflictPolicyLabel(value: string) {
  return value === "skip" ? "跳过已存在文件" : "覆盖并删除多余文件";
}

function scheduleIntervalMinutes(kind: "daily" | "weekly" | "monthly") {
  if (kind === "weekly") return 7 * 24 * 60;
  if (kind === "monthly") return 30 * 24 * 60;
  return 24 * 60;
}

function scheduleTimeSeconds() {
  return syncRuleForm.hour * 3600 + syncRuleForm.minute * 60 + syncRuleForm.second;
}

function scheduleLabel(rule: ContainerSyncRule) {
  const kind = rule.schedule_kind === "weekly" ? "每周" : rule.schedule_kind === "monthly" ? "每月" : "每天";
  const seconds = rule.schedule_time_seconds || 0;
  const h = Math.floor(seconds / 3600).toString().padStart(2, "0");
  const m = Math.floor((seconds % 3600) / 60).toString().padStart(2, "0");
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${kind} ${h}:${m}:${s}`;
}

function taskStatusType(status: string) {
  if (["succeeded", "ready"].includes(status)) return "success";
  if (["failed"].includes(status)) return "danger";
  if (["running", "planned", "verifying", "retrying"].includes(status)) return "warning";
  return "info";
}

function resourceOptions(type: "dataset" | "model" | "user_file") {
  return sharedResources.value.filter((item) => type === "dataset" ? item.resource_type === "dataset" : item.resource_type !== "dataset");
}

async function openSyncDialog(row: Container) {
  selectedContainer.value = row;
  syncDialogVisible.value = true;
  syncLoading.value = true;
  try {
    const [rules, tasks, resources] = await Promise.all([
      getContainerSyncRules(row.id),
      getContainerSyncTasks(row.id),
      getSharedResources(),
    ]);
    syncRules.value = rules;
    syncTasks.value = tasks;
    sharedResources.value = resources;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "加载同步数据失败");
  } finally {
    syncLoading.value = false;
  }
}

async function refreshSyncData() {
  if (!selectedContainer.value) return;
  const [rules, tasks] = await Promise.all([
    getContainerSyncRules(selectedContainer.value.id),
    getContainerSyncTasks(selectedContainer.value.id),
  ]);
  syncRules.value = rules;
  syncTasks.value = tasks;
}

// 公开数据集/模型下载到容器时，不需要选择存储目录，直接根目录同步
watch(() => syncDownloadForm.storage_type, (newVal) => {
  if (newVal !== "user_file") {
    syncDownloadForm.storage_relative_path = "";
    // 重置容器路径，切换类型后重新选资源时再更新
    syncDownloadForm.container_path = "/workspace";
    syncDownloadForm.resource_id = undefined;
  }
});

// 选择公开资源后自动设置容器路径为 /workspace/{资源名称}
watch(() => syncDownloadForm.resource_id, (newId) => {
  if (!newId || syncDownloadForm.storage_type === "user_file") return;
  const resource = sharedResources.value.find((r) => r.id === newId);
  if (resource) {
    syncDownloadForm.container_path = "/workspace/" + resource.name;
  }
});

async function submitDownloadSync() {
  if (!selectedContainer.value) return;
  syncSubmitting.value = true;
  try {
    await runContainerSync(selectedContainer.value.id, {
      direction: "storage_to_container",
      storage_type: syncDownloadForm.storage_type,
      resource_id: syncDownloadForm.storage_type === "user_file" ? null : syncDownloadForm.resource_id || null,
      storage_relative_path: syncDownloadForm.storage_relative_path,
      container_path: syncDownloadForm.container_path,
      conflict_policy: syncDownloadForm.conflict_policy,
    });
    ElMessage.success("下载同步任务已提交");
    await refreshSyncData();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "提交失败");
  } finally {
    syncSubmitting.value = false;
  }
}

async function submitUploadSync() {
  if (!selectedContainer.value) return;
  syncSubmitting.value = true;
  try {
    await runContainerSync(selectedContainer.value.id, {
      direction: "container_to_storage",
      storage_type: "user_file",
      storage_relative_path: syncUploadForm.storage_relative_path,
      container_path: syncUploadForm.container_path,
      conflict_policy: syncUploadForm.conflict_policy,
    });
    ElMessage.success("上传同步任务已提交");
    await refreshSyncData();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "提交失败");
  } finally {
    syncSubmitting.value = false;
  }
}

async function submitSyncRule() {
  if (!selectedContainer.value) return;
  try {
    await saveContainerSyncRule(selectedContainer.value.id, {
      name: syncRuleForm.name,
      container_path: syncRuleForm.container_path,
      storage_relative_path: syncRuleForm.storage_relative_path,
      schedule_kind: syncRuleForm.schedule_kind,
      schedule_time_seconds: scheduleTimeSeconds(),
      interval_minutes: scheduleIntervalMinutes(syncRuleForm.schedule_kind),
      enabled: syncRuleForm.enabled,
      conflict_policy: syncRuleForm.conflict_policy,
    });
    ElMessage.success("定时上传规则已保存");
    await refreshSyncData();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  }
}

async function runRule(rule: ContainerSyncRule) {
  if (!selectedContainer.value) return;
  try {
    await runContainerSyncRule(selectedContainer.value.id, rule.id);
    ElMessage.success("定时上传任务已提交");
    await refreshSyncData();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "提交失败");
  }
}

async function removeRule(rule: ContainerSyncRule) {
  if (!selectedContainer.value) return;
  await ElMessageBox.confirm(`确认删除同步规则 ${rule.name}？`, "删除同步规则", { type: "warning" });
  await deleteContainerSyncRule(selectedContainer.value.id, rule.id);
  ElMessage.success("同步规则已删除");
  await refreshSyncData();
}

function openPortDialog(container: Container, port?: ContainerPort) {
  selectedContainer.value = container;
  editingPort.value = port || null;
  portForm.name = port?.name || "";
  portForm.port_type = port?.name === "ssh" ? "ssh" : port?.name === "web" ? "web" : "custom";
  portForm.protocol = (port?.protocol || "tcp") as PortProtocol;
  portForm.container_port = port?.container_port || 22;
  if (!port) {
    portForm.name = "ssh";
    portForm.port_type = "ssh";
    portForm.protocol = "tcp";
    portForm.container_port = 22;
  }
  portDialogVisible.value = true;
}

function applyPortType() {
  if (portForm.port_type === "ssh") {
    portForm.name = "ssh";
    portForm.container_port = 22;
    portForm.protocol = "tcp";
  } else if (portForm.port_type === "web") {
    portForm.name = portForm.name && portForm.name !== "ssh" ? portForm.name : "web";
    portForm.container_port = portForm.container_port === 22 ? 80 : portForm.container_port;
    portForm.protocol = portForm.protocol === "udp" ? "tcp" : portForm.protocol;
  }
}

function portPayloads() {
  const base = {
    name: portForm.name,
    container_port: portForm.container_port
  };
  if (portForm.protocol === "both") {
    return [
      { ...base, protocol: "tcp" },
      { ...base, protocol: "udp" }
    ];
  }
  return [{ ...base, protocol: portForm.protocol }];
}

async function savePort() {
  if (!selectedContainer.value) return;
  let payloads = portPayloads();
  if (editingPort.value) {
    if (portForm.protocol === "both" && editingPort.value.protocol === "udp") {
      payloads = [payloads[1], payloads[0]];
    }
    await updateContainerPort(selectedContainer.value.id, editingPort.value.id, payloads[0]);
    for (const payload of payloads.slice(1)) {
      const exists = selectedContainer.value.ports.some(
        (port) =>
          port.id !== editingPort.value?.id &&
          port.container_port === payload.container_port &&
          port.protocol === payload.protocol
      );
      if (!exists) {
        await createContainerPort(selectedContainer.value.id, payload);
      }
    }
  } else {
    for (const payload of payloads) {
      await createContainerPort(selectedContainer.value.id, payload);
    }
  }
  ElMessage.success("端口映射已保存");
  portDialogVisible.value = false;
  await load();
}

async function handleCreated() {
  createDialogVisible.value = false;
  await load();
}

async function removePort(container: Container, port: ContainerPort) {
  await ElMessageBox.confirm(`确认删除外部端口 ${port.host_port}/${port.protocol}？`, "删除端口映射", { type: "warning" });
  await deleteContainerPort(container.id, port.id);
  ElMessage.success("端口映射已删除");
  await load();
}

function startAutoRefresh() {
  refreshTimer = window.setInterval(refreshQuietly, 3000);
}

function stopAutoRefresh() {
  if (refreshTimer) window.clearInterval(refreshTimer);
}

function handleVisibilityChange() {
  if (!document.hidden) refreshQuietly();
}

onMounted(() => {
  loadVisibleColumns();
  load();
  startAutoRefresh();
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onBeforeUnmount(() => {
  stopAutoRefresh();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>

<template>
  <el-card shadow="never" v-loading="loading">
    <template #header>
      <div class="card-header">
        <strong>容器管理</strong>
        <div class="header-actions">
          <el-popover placement="bottom-end" trigger="click" width="260">
            <template #reference>
              <el-button :icon="Setting">列设置</el-button>
            </template>
            <div class="column-settings">
              <div v-for="(column, index) in visibleColumnDefs" :key="column.key" class="column-setting-row">
                <el-checkbox :model-value="true" @change="toggleColumn(column.key, false)">
                  {{ column.label }}
                </el-checkbox>
                <div class="column-order-actions">
                  <el-button size="small" text :icon="Upload" :disabled="index === 0" @click="moveColumn(index, -1)">上移</el-button>
                  <el-button size="small" text :icon="Download" :disabled="index === visibleColumnDefs.length - 1" @click="moveColumn(index, 1)">下移</el-button>
                </div>
              </div>
              <el-divider style="margin: 8px 0" />
              <el-checkbox
                v-for="column in columnOptions.filter((item) => !visibleColumns.includes(item.key))"
                :key="column.key"
                :model-value="false"
                @change="toggleColumn(column.key, true)"
              >
                {{ column.label }}
              </el-checkbox>
              <el-button size="small" text :icon="RefreshRight" @click="resetVisibleColumns">恢复默认</el-button>
            </div>
          </el-popover>
          <el-button :icon="Refresh" @click="load">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="createDialogVisible = true">创建容器</el-button>
        </div>
      </div>
    </template>
    <el-table :data="containers" stripe>
      <el-table-column prop="name" label="名称" min-width="150" fixed />
      <el-table-column
        v-for="column in visibleColumnDefs"
        :key="column.key"
        :label="column.label"
        :width="column.key === 'owner' ? 110 : column.key === 'status' ? 145 : column.key === 'spec' ? 190 : undefined"
        :min-width="column.key === 'image_name' ? 220 : column.key === 'gpu' ? 180 : column.key === 'ports' ? 200 : column.key === 'connection' ? 250 : undefined"
      >
        <template #default="{ row }">
          <template v-if="column.key === 'owner'">{{ row.owner }}</template>
          <template v-else-if="column.key === 'image_name'">{{ row.image_name }}</template>
          <template v-else-if="column.key === 'spec'">{{ row.cpu_cores }}C / {{ row.memory_gb }}G / {{ row.disk_gb }}G</template>
          <template v-else-if="column.key === 'gpu'">{{ gpuText(row) }}</template>
          <template v-else-if="column.key === 'status'">
            <div class="status-cell">
              <StatusTag :value="row.status" />
              <el-tooltip
                v-if="accessLabel(row)"
                :content="row.access_status === 'failed' && row.access_error ? row.access_error : accessLabel(row)"
                placement="top"
                :show-after="300"
              >
                <el-tag size="small" :type="row.access_status === 'ready' ? 'success' : row.access_status === 'failed' ? 'danger' : 'warning'">
                  {{ accessLabel(row) }}
                </el-tag>
              </el-tooltip>
              <el-button
                v-if="row.status === 'running' && row.access_status === 'failed'"
                size="small"
                type="warning"
                :icon="RefreshRight"
                circle
                title="重试 SSH 准备"
                @click="retrySshAccess(row.id)"
              />
              <el-button
                v-if="row.status === 'failed'"
                size="small"
                type="warning"
                :icon="RefreshRight"
                circle
                title="重试创建"
                @click="retry(row.id)"
              />
            </div>
          </template>
          <template v-else-if="column.key === 'ports'">
            <div class="port-cell">
              <div><span class="port-label">管理</span><code>{{ portsText(row) }}</code></div>
              <div><span class="port-label">节点</span><code>{{ nodePortsText(row) }}</code></div>
            </div>
          </template>
          <template v-else-if="column.key === 'connection'">
            <div class="connection-cell">
              <code v-if="sshPort(row)">ssh {{ row.ssh_username }}@{{ publicHost() }} -p {{ publicPort(sshPort(row)!) }}</code>
              <code v-else>{{ row.ip || '-' }}</code>
              <code v-if="sshPort(row) && nodeSshCommand(row, sshPort(row)!)" class="node-port-line">
                节点 {{ nodeSshCommand(row, sshPort(row)!) }}
              </code>
            </div>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="300" fixed="right">
        <template #default="{ row }">
          <div class="container-action-panel">
            <div class="container-action-line">
              <el-button v-if="row.status === 'running'" size="small" class="container-action-button" :icon="TurnOff" :disabled="lifecycleBusy(row)" @click="action(row.id, 'stop')">停止</el-button>
              <el-button v-else size="small" class="container-action-button" :icon="VideoPlay" :disabled="lifecycleBusy(row)" @click="action(row.id, 'start')">启动</el-button>
              <el-button size="small" class="container-action-button" :icon="RefreshRight" :disabled="lifecycleBusy(row)" @click="action(row.id, 'restart')">重启</el-button>
              <el-button size="small" class="container-action-button container-action-button-wide" type="danger" :icon="Delete" @click="remove(row)">{{ lifecycleBusy(row) ? "移除" : "删除" }}</el-button>
              <el-button size="small" class="container-action-button" :icon="Setting" :disabled="lifecycleBusy(row)" @click="openResourceDialog(row)">配置</el-button>
            </div>
            <div class="container-action-line">
              <el-button size="small" class="container-action-button" :icon="Connection" :disabled="!accessReady(row)" @click="openShell(row)">Shell</el-button>
              <el-button size="small" class="container-action-button" :icon="Switch" @click="openSyncDialog(row)">同步</el-button>
              <el-button v-if="isAdmin" size="small" class="container-action-button container-action-button-wide" :icon="Upload" @click="publishImage(row)">镜像</el-button>
              <el-dropdown trigger="click">
                <el-button size="small" class="container-action-button" :icon="Tickets">端口</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="openPortDialog(row)">新增映射</el-dropdown-item>
                    <el-dropdown-item
                      v-for="port in row.ports"
                      :key="`edit-${port.id}`"
                      @click="openPortDialog(row, port)"
                    >
                      编辑 平台:{{ publicPort(port) }} -> 容器:{{ port.container_port }}/{{ port.protocol }}
                    </el-dropdown-item>
                    <el-dropdown-item
                      v-for="port in row.ports"
                      :key="`delete-${port.id}`"
                      divided
                      @click="removePort(row, port)"
                    >
                      删除 平台:{{ publicPort(port) }}
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="createDialogVisible" title="创建容器" width="980px" destroy-on-close>
    <CreateContainer embedded @created="handleCreated" />
  </el-dialog>

	  <el-dialog v-model="portDialogVisible" :title="editingPort ? '编辑端口映射' : '新增端口映射'" width="460px">
    <el-form :model="portForm" label-position="top">
      <el-form-item label="名称">
        <el-input v-model="portForm.name" placeholder="ssh / jupyter / web" />
      </el-form-item>
      <el-form-item label="端口类型">
        <el-select v-model="portForm.port_type" @change="applyPortType">
          <el-option label="SSH" value="ssh" />
          <el-option label="Web" value="web" />
          <el-option label="自定义" value="custom" />
        </el-select>
      </el-form-item>
      <el-form-item label="容器内端口">
        <el-input-number v-model="portForm.container_port" :min="1" :max="65535" />
      </el-form-item>
      <el-form-item label="协议">
        <el-select v-model="portForm.protocol">
          <el-option label="TCP" value="tcp" />
          <el-option label="UDP" value="udp" />
          <el-option label="BOTH" value="both" />
        </el-select>
      </el-form-item>
      <el-alert
        v-if="!editingPort"
        type="info"
        show-icon
        :closable="false"
        title="这里只填写容器内服务端口；平台入口端口和节点转发端口由后端随机分配。"
      />
    </el-form>
    <template #footer>
      <el-button :icon="Close" @click="portDialogVisible = false">取消</el-button>
	      <el-button type="primary" :icon="Select" @click="savePort">保存</el-button>
	    </template>
	  </el-dialog>

	  <el-dialog v-model="imageDialogVisible" :title="`镜像上传 · ${selectedContainer?.name || ''}`" width="560px">
	    <el-form :model="imageForm" label-position="top">
	      <el-form-item label="镜像 Alias">
	        <el-input v-model="imageForm.alias" placeholder="my-training-image" />
	      </el-form-item>
	      <el-form-item label="显示名称">
	        <el-input v-model="imageForm.display_name" placeholder="用于平台镜像列表展示" />
	      </el-form-item>
	      <el-form-item label="后续动作">
	        <div class="checkbox-stack">
	          <el-checkbox v-model="imageForm.export_to_storage">导出到存储节点自建镜像</el-checkbox>
	          <el-checkbox v-model="imageForm.register_platform">注册到平台可用镜像</el-checkbox>
	        </div>
	      </el-form-item>
	    </el-form>
	    <template #footer>
	      <el-button :icon="Close" @click="imageDialogVisible = false">取消</el-button>
	      <el-button type="primary" :icon="Upload" :loading="imagePublishing" @click="submitPublishImage">提交</el-button>
	    </template>
	  </el-dialog>

  <el-dialog v-model="resourceDialogVisible" :title="`修改配置 · ${selectedContainer?.name || ''}`" width="440px">
    <el-alert type="info" show-icon :closable="false" style="margin-bottom:16px"
      title="修改立即生效（容器无需重启）；GPU 变更需要容器处于 running 状态。" />
    <el-form :model="resourceForm" label-position="top">
      <el-form-item label="CPU 核数">
        <el-input-number v-model="resourceForm.cpu_cores" :min="1" :max="256" style="width:100%" />
      </el-form-item>
      <el-form-item label="内存 GB">
        <el-input-number v-model="resourceForm.memory_gb" :min="1" :max="1024" style="width:100%" />
      </el-form-item>
      <el-form-item label="GPU 数量">
        <el-input-number v-model="resourceForm.gpu_count" :min="0" :max="8" style="width:100%" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :icon="Close" @click="resourceDialogVisible = false">取消</el-button>
      <el-button type="primary" :icon="Select" :loading="resourceSaving" @click="submitResourceUpdate">保存</el-button>
    </template>
  </el-dialog>

	  <el-dialog v-model="syncDialogVisible" :title="`同步设置 · ${selectedContainer?.name || ''}`" width="920px">
	    <div v-loading="syncLoading">
	      <el-tabs>
	        <el-tab-pane label="下载到容器">
	          <el-form :model="syncDownloadForm" label-position="top" class="sync-form">
	            <el-form-item label="存储节点类型">
	              <el-segmented
	                v-model="syncDownloadForm.storage_type"
	                :options="[
	                  { label: '我的文件', value: 'user_file' },
	                  { label: '公开数据集', value: 'dataset' },
	                  { label: '公开模型', value: 'model' }
	                ]"
	              />
	            </el-form-item>
	            <el-form-item v-if="syncDownloadForm.storage_type !== 'user_file'" label="数据集/模型">
	              <el-select v-model="syncDownloadForm.resource_id" filterable style="width:100%" placeholder="选择公开资源">
	                <el-option
	                  v-for="resource in resourceOptions(syncDownloadForm.storage_type)"
	                  :key="resource.id"
	                  :label="`${resource.name}:${resource.version}`"
	                  :value="resource.id"
	                />
	              </el-select>
	            </el-form-item>
            <el-form-item v-if="syncDownloadForm.storage_type === 'user_file'" label="存储路径">
              <div class="path-picker-row">
                <el-input v-model="syncDownloadForm.storage_relative_path" placeholder="相对路径，留空表示根目录" />
                <el-button :icon="FolderOpened" @click="openStorageDirPicker('download_storage')">浏览</el-button>
              </div>
            </el-form-item>
            <el-form-item v-if="syncDownloadForm.storage_type !== 'user_file'" label="保存到容器路径">
              <el-input v-model="syncDownloadForm.container_path" placeholder="/workspace" />
            </el-form-item>
            <el-form-item v-if="syncDownloadForm.storage_type === 'user_file'" label="保存到容器路径">
              <el-input v-model="syncDownloadForm.container_path" placeholder="/workspace/data" />
            </el-form-item>
	            <el-form-item label="冲突策略">
	              <el-radio-group v-model="syncDownloadForm.conflict_policy">
	                <el-radio value="overwrite">覆盖并删除多余文件</el-radio>
	                <el-radio value="skip">跳过已存在文件</el-radio>
	              </el-radio-group>
	            </el-form-item>
	            <el-button type="primary" :icon="Download" :loading="syncSubmitting" @click="submitDownloadSync">执行下载</el-button>
	          </el-form>
	        </el-tab-pane>
	        <el-tab-pane label="上传到我的文件">
	          <el-form :model="syncUploadForm" label-position="top" class="sync-form">
	            <el-form-item label="容器路径">
	              <el-input v-model="syncUploadForm.container_path" placeholder="/workspace/output" />
	            </el-form-item>
            <el-form-item label="保存到我的文件">
              <div class="path-picker-row">
                <el-input v-model="syncUploadForm.storage_relative_path" placeholder="outputs/run-001，允许新建目录" />
                <el-button :icon="FolderOpened" @click="openStorageDirPicker('upload_storage')">浏览</el-button>
              </div>
            </el-form-item>
	            <el-form-item label="冲突策略">
	              <el-radio-group v-model="syncUploadForm.conflict_policy">
	                <el-radio value="overwrite">覆盖并删除多余文件</el-radio>
	                <el-radio value="skip">跳过已存在文件</el-radio>
	              </el-radio-group>
	            </el-form-item>
	            <el-button type="primary" :icon="Upload" :loading="syncSubmitting" @click="submitUploadSync">执行上传</el-button>
	          </el-form>
	        </el-tab-pane>
	        <el-tab-pane label="定时上传">
	          <el-form :model="syncRuleForm" label-position="top" class="sync-form">
	            <el-form-item label="规则名称"><el-input v-model="syncRuleForm.name" /></el-form-item>
	            <el-form-item label="容器路径"><el-input v-model="syncRuleForm.container_path" placeholder="/workspace/output" /></el-form-item>
	            <el-form-item label="保存到我的文件">
              <div class="path-picker-row">
                <el-input v-model="syncRuleForm.storage_relative_path" placeholder="scheduled/output，允许新建目录" />
                <el-button :icon="FolderOpened" @click="openStorageDirPicker('rule_storage')">浏览</el-button>
              </div>
            </el-form-item>
            <el-form-item label="周期">
              <div class="schedule-row">
                <el-select v-model="syncRuleForm.schedule_kind" style="width:120px">
                  <el-option label="Daily" value="daily" />
                  <el-option label="Weekly" value="weekly" />
                  <el-option label="Monthly" value="monthly" />
                </el-select>
                <el-input-number v-model="syncRuleForm.hour" :min="0" :max="23" controls-position="right" />
                <span>:</span>
                <el-input-number v-model="syncRuleForm.minute" :min="0" :max="59" controls-position="right" />
                <span>:</span>
                <el-input-number v-model="syncRuleForm.second" :min="0" :max="59" controls-position="right" />
                <el-switch v-model="syncRuleForm.enabled" active-text="启用" inactive-text="停用" />
              </div>
            </el-form-item>
            <el-form-item label="冲突策略">
              <el-radio-group v-model="syncRuleForm.conflict_policy">
                <el-radio value="overwrite">覆盖并删除多余文件</el-radio>
                <el-radio value="skip">跳过已存在文件</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-button type="primary" :icon="Select" @click="submitSyncRule">保存规则</el-button>
	          </el-form>
          <el-table :data="syncRules" stripe style="margin-top:16px">
            <el-table-column prop="name" label="名称" min-width="140" />
            <el-table-column prop="container_path" label="容器路径" min-width="170" />
            <el-table-column prop="storage_relative_path" label="我的文件路径" min-width="170" />
            <el-table-column label="冲突策略" width="120"><template #default="{ row }">{{ conflictPolicyLabel(row.conflict_policy) }}</template></el-table-column>
            <el-table-column label="时间" width="150"><template #default="{ row }">{{ scheduleLabel(row) }}</template></el-table-column>
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
	              <template #default="{ row }">
	                <el-button size="small" :icon="Position" @click="runRule(row)">立即执行</el-button>
	                <el-button size="small" type="danger" :icon="Delete" @click="removeRule(row)">删除</el-button>
	              </template>
	            </el-table-column>
	          </el-table>
	        </el-tab-pane>
	        <el-tab-pane label="任务记录">
	          <div class="card-header">
	            <strong>最近 50 条同步任务</strong>
	            <el-button :icon="Refresh" @click="refreshSyncData">刷新</el-button>
	          </div>
	          <el-table :data="syncTasks" stripe>
	            <el-table-column prop="task_type" label="类型" width="150" />
	            <el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="taskStatusType(row.status)" size="small">{{ row.status }}</el-tag></template></el-table-column>
	            <el-table-column prop="source_path" label="来源" min-width="220" show-overflow-tooltip />
	            <el-table-column prop="target_path" label="目标" min-width="220" show-overflow-tooltip />
	            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
	            <el-table-column label="错误" min-width="180" show-overflow-tooltip><template #default="{ row }">{{ row.last_error || row.detail?.error || '-' }}</template></el-table-column>
	          </el-table>
	        </el-tab-pane>
	      </el-tabs>
	    </div>
	    <template #footer>
	      <el-button :icon="CircleClose" @click="syncDialogVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- 目录选择器：我的文件 -->
  <DirectoryPicker
    ref="storageDirPicker"
    picker-type="user_file"
    :user-id="authUser?.id"
    @pick="onStorageDirPicked"
  />
</template>

<style scoped>
.status-cell {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.port-cell,
.connection-cell {
  display: grid;
  gap: 4px;
  line-height: 1.45;
}

.port-cell > div {
  display: flex;
  gap: 6px;
  align-items: flex-start;
}

.port-label {
  flex: 0 0 auto;
  color: var(--el-text-color-secondary);
}

.port-cell code,
.connection-cell code {
  white-space: normal;
  word-break: break-all;
}

.node-port-line {
  color: var(--el-text-color-secondary);
}

.sync-form {
  max-width: 640px;
}

.path-picker-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.path-picker-row .el-input {
  flex: 1;
}

.schedule-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.schedule-row :deep(.el-input-number) {
  width: 92px;
}

.checkbox-stack {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.column-settings {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.column-setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.column-order-actions {
  display: flex;
  gap: 4px;
}

.container-action-panel {
  display: grid;
  gap: 6px;
}

.container-action-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.container-action-line :deep(.el-button) {
  margin-left: 0;
  justify-content: center;
}

.container-action-line :deep(.container-action-button) {
  width: 60px;
  padding-inline: 10px;
}

.container-action-line :deep(.container-action-button-wide) {
  width: 60px;
}

.container-action-line :deep(.el-button > span) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
