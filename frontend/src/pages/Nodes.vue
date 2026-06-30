<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  Close,
  Connection,
  CopyDocument,
  Delete,
  Download,
  Finished,
  Plus,
  Refresh,
  RefreshRight,
  Select,
  Setting,
  SwitchButton,
  TurnOff,
  Upload,
} from "@element-plus/icons-vue";
import { authToken } from "../auth";
import StatusTag from "../components/StatusTag.vue";
import {
  getNodes,
  getAgentReleases,
  buildAgentRelease,
  deleteAgentRelease,
  configureAgentUpdate,
  triggerAgentUpdate,
  getUserPreference,
  deleteNode,
  nodeAction,
  updateNodeConfig,
  getNodeSshPubkey,
  installNodeSshPubkey,
  updateUserPreference,
  type Node,
  type NodeConfigPayload,
  type AgentRelease
} from "../api/cluster";
import NodeJoin from "./NodeJoin.vue";

const router = useRouter();

const loading = ref(false);
const saving = ref(false);
const addDialogVisible = ref(false);
const configDialogVisible = ref(false);
const releasesDialogVisible = ref(false);
const updateDialogVisible = ref(false);
const triggeringUpdate = ref(false);
const editingNode = ref<Node | null>(null);
const sshPubkey = ref("");
const pushingPubkey = ref(false);
const nodes = ref<Node[]>([]);
const releases = ref<AgentRelease[]>([]);
const releaseVersion = ref("");
const releaseChannel = ref<"stable" | "canary">("stable");
const releaseChangelog = ref("");
const publishing = ref(false);
const updateForm = reactive({ channel: "stable" as "stable" | "canary", auto_update: true, target_version: "" });
const columnPreferenceKey = "nodes.visible_columns";
const columnOptions = [
  { key: "ip", label: "IP", defaultVisible: true },
  { key: "status", label: "状态", defaultVisible: true },
  { key: "uptime", label: "在线时长", defaultVisible: true },
  { key: "node_type", label: "类型", defaultVisible: true },
  { key: "schedule", label: "调度", defaultVisible: true },

  { key: "driver_pool", label: "驱动池", defaultVisible: true },
  { key: "policy", label: "策略", defaultVisible: true },
  { key: "resources", label: "资源", defaultVisible: true },
  { key: "gpus", label: "GPU", defaultVisible: true },
  { key: "cuda_version", label: "CUDA", defaultVisible: true },
  { key: "incus_status", label: "Incus", defaultVisible: true },
  { key: "agent_version", label: "Agent", defaultVisible: true }
];
const defaultVisibleColumns = columnOptions.filter((column) => column.defaultVisible).map((column) => column.key);
const visibleColumns = ref<string[]>(defaultVisibleColumns);

const configForm = reactive<NodeConfigPayload>({
  node_type: "compute",
  schedulable: true,
  maintenance: false,
  max_containers: 8,
  max_running_containers: 8,
  max_gpu_shared_containers: 4,
  allow_gpu_sharing: true,
  max_cpu_per_container: 0,
  max_memory_gb_per_container: 0,
  max_disk_gb_per_container: 0,
  reserved_memory_gb: 0,
  reserved_disk_gb: 0,
  allow_port_mapping: true,
  max_ports_per_container: 8,
  scheduler_weight: 0,
  labels: [],
  wol_mac: "",
  wol_broadcast: "255.255.255.255",
  ssh_user: "root",
  ssh_port: 22,
  sync_ip: "",
  sync_ssh_port: 0
});
const labelText = ref("");
const currentTs = ref(Math.floor(Date.now() / 1000));
let clockTimer: ReturnType<typeof setInterval> | null = null;

function sanitizeColumns(value: unknown) {
  const validColumns = new Set(columnOptions.map((column) => column.key));
  if (Array.isArray(value)) {
    return value.filter((key): key is string => typeof key === "string" && validColumns.has(key));
  }
  return [];
}

function orderedColumns(keys: string[]) {
  const selected = new Set(keys);
  return [
    ...keys.filter((key) => columnOptions.some((column) => column.key === key)),
    ...columnOptions.filter((column) => !selected.has(column.key)).map((column) => column.key)
  ];
}

function formatBytes(bytes: number) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = Math.max(0, bytes || 0);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 || size >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatGb(value: number) {
  return formatBytes((value || 0) * 1024 * 1024 * 1024);
}

function formatDuration(totalSeconds: number) {
  let seconds = Math.max(0, Math.floor(totalSeconds || 0));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function nodeUptimeText(row: Node) {
  if (!nodeOnline(row)) return "离线";
  if (row.uptime_seconds > 0) {
    const elapsedSinceHeartbeat = Math.max(0, currentTs.value - (row.last_seen || currentTs.value));
    return `在线 ${formatDuration(row.uptime_seconds + elapsedSinceHeartbeat)}`;
  }
  return "在线";
}

const visibleColumnDefs = computed(() => {
  const byKey = new Map(columnOptions.map((column) => [column.key, column]));
  return visibleColumns.value.map((key) => byKey.get(key)).filter((column): column is (typeof columnOptions)[number] => Boolean(column));
});

const latestVersion = computed(() => releases.value[0]?.version ?? "");

function nodeOnline(row: Node) {
  return row.status === "online";
}

function nodeWakeEnabled(row: Node) {
  return Boolean(row.wol_mac);
}

async function loadVisibleColumns() {
  try {
    const preference = await getUserPreference<{ columns?: unknown[] }>(columnPreferenceKey);
    const storedColumns = sanitizeColumns(preference.value.columns);
    visibleColumns.value = storedColumns.length
      ? orderedColumns([...storedColumns, ...defaultVisibleColumns.filter((key) => key === "uptime" && !storedColumns.includes(key))])
      : defaultVisibleColumns;
  } catch (error) {
    ElMessage.warning(error instanceof Error ? error.message : "列设置读取失败，已使用默认列");
    visibleColumns.value = defaultVisibleColumns;
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
  visibleColumns.value = defaultVisibleColumns;
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

async function load() {
  loading.value = true;
  try {
    [nodes.value, releases.value] = await Promise.all([getNodes(), getAgentReleases()]);
  } finally {
    loading.value = false;
  }
}

async function openReleases() {
  releasesDialogVisible.value = true;
  releases.value = await getAgentReleases();
}

async function publishRelease() {
  if (!releaseVersion.value.trim()) {
    ElMessage.warning("请填写版本号");
    return;
  }
  publishing.value = true;
  try {
    await buildAgentRelease(releaseVersion.value.trim(), releaseChannel.value, releaseChangelog.value.trim());
    releases.value = await getAgentReleases();
    releaseVersion.value = "";
    releaseChangelog.value = "";
    ElMessage.success("Agent 编译完成，版本已注册");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "编译失败");
  } finally {
    publishing.value = false;
  }
}

async function downloadAgentFile(url: string, filename: string) {
  try {
    const resp = await fetch(url, { headers: { Authorization: `Bearer ${authToken.value}` } });
    if (!resp.ok) throw new Error(`下载失败 (${resp.status})`);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "下载失败");
  }
}

async function downloadRelease(row: AgentRelease) {
  await downloadAgentFile(
    `/api/agent-releases/${encodeURIComponent(row.version)}/download?architecture=${row.architecture}`,
    `cluster-node-agent-${row.version}-${row.architecture}`
  );
}

async function downloadLatestAgent() {
  await downloadAgentFile("/api/agent-releases/latest/download?architecture=amd64", "cluster-node-agent");
}

async function downloadLatestUpdater() {
  await downloadAgentFile("/api/agent-releases/latest/download-updater?architecture=amd64", "cluster-agent-updater");
}

async function removeRelease(row: AgentRelease) {
  try {
    await ElMessageBox.confirm(
      `确认删除 agent 版本 ${row.version}（${row.architecture}）？该操作将同时删除服务器上的二进制文件。`,
      "删除确认",
      { type: "warning" }
    );
  } catch {
    return;
  }
  try {
    await deleteAgentRelease(row.version);
    releases.value = await getAgentReleases();
    ElMessage.success("已删除");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

async function openAgentUpdate(row: Node, targetVersion?: string) {
  editingNode.value = row;
  Object.assign(updateForm, {
    channel: row.agent_update_channel || "stable",
    auto_update: row.agent_auto_update ?? true,
    target_version: targetVersion ?? row.target_agent_version ?? ""
  });
  updateDialogVisible.value = true;
}

async function saveAgentUpdate() {
  if (!editingNode.value) return;
  try {
    await configureAgentUpdate(editingNode.value.id, updateForm);
    ElMessage.success("Agent 更新策略已保存，更新器将在 15 分钟内自动检查（由 systemd timer 每 15 分钟触发）");
    updateDialogVisible.value = false;
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  }
}

async function doTriggerUpdate(node: Node) {
  if (!nodeOnline(node)) {
    ElMessage.warning("离线节点不能触发 Agent 更新");
    return;
  }
  triggeringUpdate.value = true;
  try {
    await triggerAgentUpdate(node.id);
    ElMessage.success(`已向 ${node.hostname} 下发立即更新任务`);
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "触发失败");
  } finally {
    triggeringUpdate.value = false;
  }
}

function openConfig(row: Node) {
  editingNode.value = row;
  Object.assign(configForm, {
    node_type: row.node_type,
    schedulable: row.schedulable,
    maintenance: row.maintenance,
    max_containers: row.max_containers,
    max_running_containers: row.max_running_containers,
    max_gpu_shared_containers: row.max_gpu_shared_containers,
    allow_gpu_sharing: row.allow_gpu_sharing,
    max_cpu_per_container: row.max_cpu_per_container,
    max_memory_gb_per_container: row.max_memory_gb_per_container,
    max_disk_gb_per_container: row.max_disk_gb_per_container,
    reserved_memory_gb: row.reserved_memory_gb,
    reserved_disk_gb: row.reserved_disk_gb,
    allow_port_mapping: row.allow_port_mapping,
    max_ports_per_container: row.max_ports_per_container,
    scheduler_weight: row.scheduler_weight,
    labels: [...(row.labels || [])],
    wol_mac: row.wol_mac || "",
    wol_broadcast: row.wol_broadcast || "255.255.255.255",
    ssh_user: row.ssh_user || "root",
    ssh_port: row.ssh_port || 22,
    sync_ip: row.sync_ip || "",
    sync_ssh_port: row.sync_ssh_port || 0
  });
  labelText.value = (row.labels || []).join(", ");
  getNodeSshPubkey().then((res) => { sshPubkey.value = res.pubkey; }).catch(() => {});
  configDialogVisible.value = true;
}

async function saveConfig() {
  if (!editingNode.value) return;
  saving.value = true;
  try {
    configForm.labels = labelText.value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const updated = await updateNodeConfig(editingNode.value.id, configForm);
    const index = nodes.value.findIndex((node) => node.id === updated.id);
    if (index >= 0) nodes.value[index] = updated;
    ElMessage.success("节点配置已保存");
    configDialogVisible.value = false;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

async function shutdownNode(row: Node) {
  if (!nodeOnline(row)) {
    ElMessage.warning("离线节点不能执行关机操作");
    return;
  }
  try {
    const result = await ElMessageBox.prompt(
      `关机会让节点 ${row.hostname} 立即下线，现有容器也会停止。请输入节点名称确认。`,
      "关机节点",
      {
        type: "warning",
        confirmButtonText: "关机",
        cancelButtonText: "取消",
        inputPattern: new RegExp(`^${row.hostname.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`),
        inputErrorMessage: "请输入完整节点名称"
      }
    );
    if (result.value !== row.hostname) return;
    await nodeAction(row.id, "shutdown");
    ElMessage.success("关机任务已提交");
    await load();
  } catch {
    // 用户取消
  }
}

async function rebootNode(row: Node) {
  if (!nodeOnline(row)) {
    ElMessage.warning("离线节点不能执行重启操作");
    return;
  }
  try {
    await ElMessageBox.confirm(`确认重启节点 ${row.hostname}？节点会短暂离线。`, "重启节点", {
      type: "warning",
      confirmButtonText: "重启",
      cancelButtonText: "取消"
    });
    await nodeAction(row.id, "reboot");
    ElMessage.success("重启任务已提交");
    await load();
  } catch {
    // 用户取消
  }
}

async function wakeNode(row: Node) {
  if (!nodeWakeEnabled(row)) {
    ElMessage.warning("请先在节点配置里填写 WOL MAC 地址");
    return;
  }
  await nodeAction(row.id, "wake");
  ElMessage.success("LAN 唤醒包已发送");
}

function copySshPubkey() {
  if (!sshPubkey.value) return;
  navigator.clipboard.writeText(sshPubkey.value).then(() => ElMessage.success("已复制"));
}

async function pushSshPubkey() {
  if (!editingNode.value || !sshPubkey.value) return;
  pushingPubkey.value = true;
  try {
    await installNodeSshPubkey(editingNode.value.id);
    ElMessage.success("公鉅推送任务已提交，agent 将在下一次心跳后执行");
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : "推送失败");
  } finally {
    pushingPubkey.value = false;
  }
}

async function removeNode(row: Node) {
  const result = await ElMessageBox.prompt(
    `此操作只会移除平台中的节点记录，不会卸载新机器上的 agent 或 Incus。若该节点仍有关联容器，后端会拒绝删除。请输入节点名称 ${row.hostname} 确认移除。`,
    "移除节点",
    {
      type: "warning",
      confirmButtonText: "移除",
      cancelButtonText: "取消",
      inputPattern: new RegExp(`^${row.hostname.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`),
      inputErrorMessage: "请输入完整节点名称"
    }
  );
  if (result.value !== row.hostname) return;
  await deleteNode(row.id);
  ElMessage.success("节点记录已移除");
  await load();
}

onMounted(() => {
  loadVisibleColumns();
  load();
  clockTimer = setInterval(() => {
    currentTs.value = Math.floor(Date.now() / 1000);
  }, 60000);
});

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer);
});
</script>

<template>
  <div>
    <el-card shadow="never" v-loading="loading">
      <template #header>
        <div class="card-header">
          <strong>节点管理</strong>
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
            <el-button :icon="Upload" @click="openReleases">Agent 发布</el-button>
            <el-button type="primary" :icon="Plus" @click="addDialogVisible = true">添加节点</el-button>
          </div>
        </div>
      </template>
      <el-table :data="nodes" stripe>
        <el-table-column prop="hostname" label="主机名" min-width="160" fixed />
        <el-table-column
          v-for="column in visibleColumnDefs"
          :key="column.key"
          :prop="column.key"
          :label="column.label"
          :min-width="column.key === 'gpus' ? 260 : column.key === 'policy' || column.key === 'resources' ? 220 : column.key === 'driver_pool' || column.key === 'uptime' ? 150 : 130"
          :width="column.key === 'uptime' ? 150 : ['status', 'node_type', 'schedule', 'incus_status'].includes(column.key) ? 110 : undefined"
        >
          <template #default="{ row }">
            <template v-if="column.key === 'ip'">{{ row.ip }}</template>
            <StatusTag v-else-if="column.key === 'status'" :value="row.status" />
            <template v-else-if="column.key === 'uptime'">
              <el-tag :type="nodeOnline(row) ? 'success' : 'info'" size="small" round>{{ nodeUptimeText(row) }}</el-tag>
            </template>
            <template v-else-if="column.key === 'node_type'">{{ row.node_type }}</template>
            <template v-else-if="column.key === 'schedule'">
              <el-tag :type="row.schedulable && !row.maintenance ? 'success' : 'warning'" round>
                {{ row.maintenance ? "维护" : row.schedulable ? "启用" : "关闭" }}
              </el-tag>
            </template>
            <template v-else-if="column.key === 'driver_pool'">{{ row.driver_pool }}</template>
            <template v-else-if="column.key === 'policy'">
              容器 {{ row.max_running_containers }}/{{ row.max_containers }} ·
              GPU {{ row.allow_gpu_sharing ? `共享 ${row.max_gpu_shared_containers}` : "独占" }} ·
              权重 {{ row.scheduler_weight }}
            </template>
            <template v-else-if="column.key === 'resources'">
              {{ row.cpu_used }}/{{ row.cpu_total }} CPU ·
              {{ row.memory_used_gb }}/{{ row.memory_total_gb }} GB ·
              {{ formatGb(row.disk_used_gb) }}/{{ formatGb(row.disk_total_gb) }}
            </template>
            <template v-else-if="column.key === 'gpus'">
              <div class="chip-row">
                <el-tag v-for="gpu in row.gpus" :key="gpu.id" :type="gpu.container ? 'success' : 'info'" round>
                  {{ gpu.model }} #{{ gpu.slot }}
                </el-tag>
              </div>
            </template>
            <template v-else-if="column.key === 'incus_status'">{{ row.incus_status }}</template>
            <template v-else-if="column.key === 'cuda_version'">
              <el-tag v-if="row.cuda_driver_api_version" class="cuda-tag" effect="dark" round>{{ row.cuda_driver_api_version }}</el-tag>
              <span v-else>—</span>
            </template>
            <template v-else-if="column.key === 'agent_version'">
              <div>{{ row.agent_version || "未知" }}</div>
              <el-tag v-if="latestVersion && row.agent_version === latestVersion" type="success" size="small">最新版本</el-tag>
              <el-button
                v-else-if="latestVersion && row.agent_version"
                size="small"
                type="warning"
                :icon="RefreshRight"
                :disabled="!nodeOnline(row)"
                @click="doTriggerUpdate(row)"
              >立即更新</el-button>
              <el-tag
                v-if="row.agent_update_status && row.agent_update_status !== 'idle' && row.agent_update_status !== 'updated'"
                size="small"
                :type="row.agent_update_status === 'failed' || row.agent_update_status === 'rolled_back' ? 'danger' : 'warning'"
              >{{ row.agent_update_status }}</el-tag>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <div class="node-action-col">
              <div class="node-action-row">
                <el-button size="small" class="node-action-button" type="warning" :icon="TurnOff" :disabled="!nodeOnline(row)" @click="shutdownNode(row)">关机</el-button>
                <el-button size="small" class="node-action-button" :icon="RefreshRight" :disabled="!nodeOnline(row)" @click="rebootNode(row)">重启</el-button>
                <el-button size="small" class="node-action-button node-action-button-wide" :icon="SwitchButton" :disabled="!nodeWakeEnabled(row)" @click="wakeNode(row)">LAN 唤醒</el-button>
              </div>
              <div class="node-action-row">
                <el-button size="small" class="node-action-button" :icon="Connection" :disabled="!nodeOnline(row)" @click="router.push({ name: 'nodeShell', params: { id: row.id } })">Shell</el-button>
                <el-button size="small" class="node-action-button" :icon="Setting" @click="openConfig(row)">配置</el-button>
                <el-button size="small" class="node-action-button" type="danger" :icon="Delete" @click="removeNode(row)">移除</el-button>
              </div>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="addDialogVisible" title="添加节点" width="980px" destroy-on-close>
      <NodeJoin />
    </el-dialog>

    <el-dialog v-model="configDialogVisible" :title="`节点配置 · ${editingNode?.hostname || ''}`" width="860px">
      <el-form :model="configForm" label-position="top" class="config-grid">
        <el-form-item label="节点类型">
          <el-select v-model="configForm.node_type">
            <el-option label="计算节点" value="compute" />
            <el-option label="存储节点" value="storage" />
            <el-option label="应用服务节点" value="app" />
            <el-option label="混合节点" value="mixed" />
          </el-select>
        </el-form-item>
        <el-form-item label="参与调度">
          <el-switch v-model="configForm.schedulable" />
        </el-form-item>
        <el-form-item label="维护模式">
          <el-switch v-model="configForm.maintenance" />
        </el-form-item>
        <el-form-item label="允许 GPU 共享">
          <el-switch v-model="configForm.allow_gpu_sharing" />
        </el-form-item>
        <el-form-item label="最大容器数量">
          <el-input-number v-model="configForm.max_containers" :min="0" :max="500" />
        </el-form-item>
        <el-form-item label="最大运行中容器">
          <el-input-number v-model="configForm.max_running_containers" :min="0" :max="500" />
        </el-form-item>
        <el-form-item label="每张 GPU 最大共享容器">
          <el-input-number v-model="configForm.max_gpu_shared_containers" :min="0" :max="100" />
        </el-form-item>
        <el-form-item label="调度权重">
          <el-input-number v-model="configForm.scheduler_weight" :min="-1000" :max="1000" />
        </el-form-item>
        <el-form-item label="单容器最大 CPU">
          <el-input-number v-model="configForm.max_cpu_per_container" :min="0" :max="1024" />
        </el-form-item>
        <el-form-item label="单容器最大内存 GB">
          <el-input-number v-model="configForm.max_memory_gb_per_container" :min="0" :max="8192" />
        </el-form-item>
        <el-form-item label="单容器最大磁盘 GB">
          <el-input-number v-model="configForm.max_disk_gb_per_container" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="保留内存 GB">
          <el-input-number v-model="configForm.reserved_memory_gb" :min="0" :max="8192" />
        </el-form-item>
        <el-form-item label="保留磁盘 GB">
          <el-input-number v-model="configForm.reserved_disk_gb" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="允许端口映射">
          <el-switch v-model="configForm.allow_port_mapping" />
        </el-form-item>
        <el-form-item label="单容器最大端口数">
          <el-input-number v-model="configForm.max_ports_per_container" :min="0" :max="1000" />
        </el-form-item>
        <el-form-item label="标签" class="wide">
          <el-input v-model="labelText" placeholder="p40, ssd, app-zone" />
        </el-form-item>
        <el-form-item label="WOL MAC 地址">
          <el-input v-model="configForm.wol_mac" placeholder="aa:bb:cc:dd:ee:ff" />
        </el-form-item>
        <el-form-item label="WOL 广播地址">
          <el-input v-model="configForm.wol_broadcast" placeholder="255.255.255.255" />
        </el-form-item>
        <el-form-item label="Shell SSH 用户">
          <el-input v-model="configForm.ssh_user" placeholder="root" />
        </el-form-item>
        <el-form-item label="Shell SSH 端口">
          <el-input-number v-model="configForm.ssh_port" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item
          v-if="editingNode?.node_type === 'storage' || editingNode?.node_type === 'mixed'"
          label="数据同步 IP"
        >
          <el-input v-model="configForm.sync_ip" placeholder="留空使用节点上报 IP" />
          <div style="margin-top:4px;color:var(--el-color-info);font-size:12px">
            rsync / 镜像分发时优先使用该地址，解决内网 IP 对计算节点不可达的问题
          </div>
        </el-form-item>
        <el-form-item
          v-if="editingNode?.node_type === 'storage' || editingNode?.node_type === 'mixed'"
          label="数据同步 SSH 端口"
        >
          <el-input-number v-model="configForm.sync_ssh_port" :min="0" :max="65535" />
          <div style="margin-top:4px;color:var(--el-color-info);font-size:12px">
            0 表示回退到 Shell SSH 端口
          </div>
        </el-form-item>
        <el-form-item label="平台 SSH 公钥" class="wide">
          <el-input :value="sshPubkey || '尚未生成，打开配置面板后将自动获取'" readonly>
            <template #append>
              <el-button :icon="CopyDocument" @click="copySshPubkey">复制</el-button>
              <el-button :icon="Upload" :loading="pushingPubkey" :disabled="!sshPubkey" @click="pushSshPubkey">推送到节点</el-button>
            </template>
          </el-input>
          <div style="margin-top:4px;color:var(--el-color-info);font-size:12px">「推送到节点」会通过 agent 自动将公鉅写入节点 <code>~/.ssh/authorized_keys</code></div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="configDialogVisible = false">取消</el-button>
        <el-button type="primary" :icon="Select" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="releasesDialogVisible" title="Agent 版本管理" width="800px">
      <el-alert type="info" :closable="false" style="margin-bottom:16px">
        <template #title>如何构建？</template>
        填写版本号并选择通道，点击「构建」后后端将自动调用 Docker 启动 <code>golang:1.23-alpine</code> 容器编译最新源码，编译产物直接写入发布目录。首次构建需下载 Go 模块，可能需要较长时间，请耐心等待。
      </el-alert>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-bottom:16px">
        <el-button :icon="Download" data-keep-label="true" @click="downloadLatestAgent">下载最新版 Agent</el-button>
        <el-button :icon="Download" data-keep-label="true" @click="downloadLatestUpdater">下载 Updater</el-button>
      </div>
      <el-form label-position="top">
        <div style="display:flex;gap:12px;align-items:flex-end">
          <el-form-item label="版本号" style="margin-bottom:0;flex:1"><el-input v-model="releaseVersion" placeholder="0.3.0" /></el-form-item>
          <el-form-item label="通道" style="margin-bottom:0">
            <el-select v-model="releaseChannel" style="width:120px"><el-option label="Stable" value="stable" /><el-option label="Canary" value="canary" /></el-select>
          </el-form-item>
          <el-form-item label=" " style="margin-bottom:0"><el-button type="primary" :icon="Finished" :loading="publishing" @click="publishRelease">{{ publishing ? '编译中…' : '构建' }}</el-button></el-form-item>
        </div>
        <el-form-item label="版本更新内容" style="margin-top:12px;margin-bottom:0">
          <el-input v-model="releaseChangelog" type="textarea" :rows="3" placeholder="本版本的功能变更、修复说明（可选）" />
        </el-form-item>
      </el-form>
      <el-divider />
      <el-table :data="releases" max-height="300">
        <el-table-column prop="version" label="版本" width="100" />
        <el-table-column prop="channel" label="通道" width="80" />
        <el-table-column prop="architecture" label="架构" width="80" />
        <el-table-column label="大小" width="90"><template #default="{ row }">{{ (row.size_bytes / 1024 / 1024).toFixed(1) }} MB</template></el-table-column>
        <el-table-column label="SHA256" width="150"><template #default="{ row }">{{ row.sha256.slice(0, 14) }}…</template></el-table-column>
        <el-table-column label="更新内容" min-width="140">
          <template #default="{ row }">
            <el-tooltip v-if="row.changelog" :content="row.changelog" placement="top" :show-after="300">
              <span style="cursor:default;color:var(--el-color-primary)">{{ row.changelog.length > 30 ? row.changelog.slice(0, 30) + '…' : row.changelog }}</span>
            </el-tooltip>
            <span v-else style="color:var(--el-text-color-placeholder)">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" align="center">
          <template #default="{ row }">
            <div style="display:flex;gap:8px;justify-content:center">
              <el-button size="small" :icon="Download" @click="downloadRelease(row)">下载</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="removeRelease(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog v-model="updateDialogVisible" :title="`Agent 更新 · ${editingNode?.hostname || ''}`" width="520px">
      <el-form label-position="top">
        <el-form-item label="启用自动更新"><el-switch v-model="updateForm.auto_update" /></el-form-item>
        <el-form-item label="更新通道">
          <el-select v-model="updateForm.channel"><el-option label="Stable" value="stable" /><el-option label="Canary" value="canary" /></el-select>
        </el-form-item>
        <el-form-item label="固定目标版本">
          <el-select v-model="updateForm.target_version" clearable placeholder="留空则跟随通道最新版本">
            <el-option v-for="release in releases" :key="`${release.version}-${release.architecture}`" :label="`${release.version} (${release.channel})`" :value="release.version" />
          </el-select>
        </el-form-item>
        <el-alert v-if="editingNode?.agent_update_error" type="error" :closable="false" :title="editingNode.agent_update_error" />
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="updateDialogVisible = false">取消</el-button>
        <el-button type="warning" :icon="RefreshRight" :loading="triggeringUpdate" :disabled="!editingNode" @click="editingNode && doTriggerUpdate(editingNode).then(() => { updateDialogVisible = false })">立即触发更新</el-button>
        <el-button type="primary" :icon="Select" @click="saveAgentUpdate">保存策略</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cuda-tag {
  border-color: #0891b2;
  background: #0891b2;
  color: #fff;
}

.node-action-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.node-action-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.node-action-row :deep(.el-button) {
  margin-left: 0;
  justify-content: center;
}

.node-action-row :deep(.node-action-button) {
  width: 60px;
  padding-inline: 10px;
}

.node-action-row :deep(.node-action-button-wide) {
  width: 80px;
}

.node-action-row :deep(.el-button > span) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px 16px;
}

.config-grid :deep(.el-form-item) {
  margin-bottom: 0;
}

.config-grid .wide {
  grid-column: 1 / -1;
}

@media (max-width: 900px) {
  .config-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
