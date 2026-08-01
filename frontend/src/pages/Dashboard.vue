<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import StatCard from "../components/StatCard.vue";
import MonitoringPage from "./Monitoring.vue";
import { hasAdminAccess } from "../auth";
import { getGpus, getNodeHardware, getSummary, type Gpu, type NodeHardware, type Summary } from "../api/cluster";

const { t } = useI18n();
const isAdmin = computed(() => hasAdminAccess());
const SPARKLINE_MAX = 30; // 保留最近 30 个采样点（2s 间隔 = 1 分钟历史）

const loading = ref(false);
const monitorRefreshing = ref(false);
const summary = ref<Summary | null>(null);
const gpus = ref<Gpu[]>([]);
const hardwareRows = ref<NodeHardware[]>([]);
const currentTs = ref(Math.floor(Date.now() / 1000));
// 每个节点的 CPU% 历史（node.id → number[]）
const cpuHistory = ref(new Map<number, number[]>());
let refreshTimer: number | undefined;
let clockTimer: number | undefined;

function pushCpuHistory(nodes: NodeHardware[]) {
  const map = cpuHistory.value;
  for (const node of nodes) {
    const history = map.get(node.id) ?? [];
    history.push(Math.min(100, Math.max(0, node.cpu_usage_percent ?? 0)));
    if (history.length > SPARKLINE_MAX) history.splice(0, history.length - SPARKLINE_MAX);
    map.set(node.id, history);
  }
  // 触发响应式更新
  cpuHistory.value = new Map(map);
}

// SVG area chart: 折线下方填充，上方空白
// 返回 { linePoints, areaPoints } 用于 polyline 和 polygon
function sparklinePoints(data: number[], w = 80, h = 28): { linePoints: string; areaPoints: string } {
  if (data.length < 2) return { linePoints: "", areaPoints: "" };
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / 100) * h).toFixed(1)}`);
  const linePoints = pts.join(" ");
  // 面积图：上方折线点 + 右下角 + 左下角 构成填充多边形
  const areaPoints = [...pts, `${w.toFixed(1)},${h}`, `0,${h}`].join(" ");
  return { linePoints, areaPoints };
}

function sparklineColor(lastPct: number): string {
  if (lastPct < 50) return "var(--el-color-success)";
  if (lastPct < 80) return "var(--el-color-warning)";
  return "var(--el-color-danger)";
}

function pct(used: number, total: number) {
  if (!total) return 0;
  return Math.min(100, Math.round((used / total) * 100));
}

function pctColor(p: number): string {
  if (p < 60) return "var(--el-color-success)";
  if (p < 85) return "var(--el-color-warning)";
  return "var(--el-color-danger)";
}

function gpuState(utilization: number) {
  if (utilization < 5) return { label: t("dashboard.idle"), type: "success" as const };
  if (utilization < 50) return { label: t("dashboard.working"), type: "warning" as const };
  return { label: t("dashboard.busy"), type: "danger" as const };
}

function gpuContainers(row: Gpu) {
  return row.containers?.length ? row.containers : row.container ? [row.container] : [];
}

function metricNumber(value: number | undefined) { return value ?? 0; }

function formatBytes(bytes: number) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = Math.max(0, bytes || 0);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index === 0 || size >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatGb(value: number) { return formatBytes((value || 0) * 1024 * 1024 * 1024); }

function formatGbNum(value: number) {
  const gb = (value || 0);
  if (gb >= 1024) {
    return `${(gb / 1024).toFixed(gb >= 10240 ? 0 : 1)} TB`;
  }
  if (gb >= 1) {
    return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
  }
  return `${(gb * 1024).toFixed(0)} MB`;
}

function formatGbPair(used: number, total: number) {
  return `${formatGb(used)} / ${formatGb(total)}`;
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

function nodeUptimeText(row: NodeHardware) {
  if (row.status !== "online") return t("dashboard.offline");
  if (row.uptime_seconds > 0) {
    const elapsedSinceHeartbeat = Math.max(0, currentTs.value - (row.last_seen || currentTs.value));
    return t("dashboard.onlineDuration", { duration: formatDuration(row.uptime_seconds + elapsedSinceHeartbeat) });
  }
  return t("dashboard.online");
}

/** 显存已用/总量，输入单位 MB，输出自适应单位 */
function formatMbPair(usedMb: number, totalMb: number) {
  const used = usedMb || 0;
  const total = totalMb || 0;
  if (total >= 1024 * 1024) {
    return `${(used / 1024 / 1024).toFixed(1)} / ${(total / 1024 / 1024).toFixed(1)} TB`;
  }
  if (total >= 1024) {
    return `${(used / 1024).toFixed(total >= 10240 ? 0 : 1)} / ${(total / 1024).toFixed(total >= 10240 ? 0 : 1)} GB`;
  }
  return `${used.toFixed(0)} / ${total.toFixed(0)} MB`;
}

// 节点的 GPU 列表（从 gpus 全局列表里按 hostname 匹配）
function nodeGpus(row: NodeHardware): Gpu[] {
  return gpus.value.filter(g => g.hostname === row.hostname);
}

function formatCpuHardware(row: NodeHardware): string[] {
  const sockets = Math.max(1, row.cpu_sockets || 1);
  const model = row.cpu_model || "CPU";
  const line1 = sockets > 1 ? `${sockets} × ${model}` : model;

  const coresPerSocket = row.cpu_cores || 0;
  const threadsPerSocket =
    sockets > 1 && row.cpu_total > 0
      ? Math.floor(row.cpu_total / sockets)
      : row.cpu_total || 0;

  const line2Parts: string[] = [];

  if (sockets > 1) {
    const perSocket = [
      coresPerSocket > 0 ? t("dashboard.cores", { count: coresPerSocket }) : "",
      threadsPerSocket > 0 ? t("dashboard.threads", { count: threadsPerSocket }) : "",
    ].filter(Boolean).join(" / ");

    const totalCores = coresPerSocket > 0 ? coresPerSocket * sockets : 0;
    const total = [
      totalCores > 0 ? t("dashboard.cores", { count: totalCores }) : "",
      row.cpu_total > 0 ? t("dashboard.threads", { count: row.cpu_total }) : "",
    ].filter(Boolean).join(" / ");

    if (perSocket) line2Parts.push(t("dashboard.perSocket", { value: perSocket }));
    if (total) line2Parts.push(t("dashboard.total", { value: total }));
  } else {
    // 单插槽统一为 "x核 / y线程"
    const singleSocket = [
      coresPerSocket > 0 ? t("dashboard.cores", { count: coresPerSocket }) : "",
      row.cpu_total > 0 ? t("dashboard.threads", { count: row.cpu_total }) : "",
    ].filter(Boolean).join(" / ");

    if (singleSocket) line2Parts.push(singleSocket);
  }

  const line2 = line2Parts.join(" · ");

  return line2 ? [line1, line2] : [line1];
}

function formatGpuHardware(row: NodeHardware) {
  if (!row.gpus.length) return t("dashboard.none");
  const groups = new Map<string, number>();
  for (const gpu of row.gpus) {
    const label = `${gpu.model || t("dashboard.unknownGpu")} ${gpu.vram_gb || 0} GB`;
    groups.set(label, (groups.get(label) || 0) + 1);
  }
  return Array.from(groups.entries()).map(([label, count]) => count > 1 ? `${label} x${count}` : label).join(" · ");
}

async function refreshMonitor() {
  monitorRefreshing.value = true;
  try {
    const [summaryResult, gpuResult, hwResult] = await Promise.all([getSummary(), getGpus(), getNodeHardware()]);
    summary.value = summaryResult;
    gpus.value = gpuResult;
    hardwareRows.value = hwResult;
    pushCpuHistory(hwResult);
  } finally {
    monitorRefreshing.value = false;
  }
}

async function loadInitial() {
  loading.value = true;
  try {
    const [summaryResult, gpuResult, hardwareResult] = await Promise.all([getSummary(), getGpus(), getNodeHardware()]);
    summary.value = summaryResult;
    gpus.value = gpuResult;
    hardwareRows.value = hardwareResult;
    pushCpuHistory(hardwareResult);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  loadInitial();
  refreshTimer = window.setInterval(refreshMonitor, 2000);
  clockTimer = window.setInterval(() => {
    currentTs.value = Math.floor(Date.now() / 1000);
  }, 60000);
});

onUnmounted(() => {
  if (refreshTimer) window.clearInterval(refreshTimer);
  if (clockTimer) window.clearInterval(clockTimer);
});
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <!-- 集群告警条 -->
    <template v-if="summary && summary.alerts.length > 0">
      <el-alert
        v-for="(alert, i) in summary.alerts"
        :key="i"
        :type="alert.level === 'error' ? 'error' : alert.level === 'warning' ? 'warning' : 'info'"
        :title="alert.message"
        show-icon
        closable
        style="margin-bottom: 4px;"
      />
    </template>

    <div v-if="summary" class="stats-grid">
      <StatCard :label="t('dashboard.onlineNodes')" :value="`${summary.nodes_online}/${summary.nodes_total}`" :detail="t('dashboard.registeredServers')" />
      <StatCard :label="t('dashboard.availableGpu')" :value="`${summary.gpus_free}/${summary.gpus_total}`" :detail="t('dashboard.gpuSharing')" />
      <StatCard :label="t('dashboard.runningContainers')" :value="`${summary.containers_running} / ${summary.containers_total}`" :detail="t('dashboard.runningTotal')" />
      <StatCard label="CPU" :value="`${pct(summary.cpu_used, summary.cpu_total)}%`" :detail="t('dashboard.cpuCores', { used: summary.cpu_used, total: summary.cpu_total })" />
      <StatCard :label="t('dashboard.memory')" :value="`${pct(summary.memory_used_gb, summary.memory_total_gb)}%`" :detail="`${formatGb(summary.memory_used_gb)} / ${formatGb(summary.memory_total_gb)}`" />
      <StatCard :label="t('dashboard.disk')" :value="`${pct(summary.disk_used_gb, summary.disk_total_gb)}%`" :detail="`${formatGb(summary.disk_used_gb)} / ${formatGb(summary.disk_total_gb)}`" />
    </div>

    <!-- 节点实时监控图表卡片 -->
    <el-tabs>
      <el-tab-pane :label="t('dashboard.realtimeMonitor')">
        <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <strong>{{ t("dashboard.realtimeMonitor") }}</strong>
          <el-button :icon="Refresh" :loading="monitorRefreshing" @click="refreshMonitor">{{ t("common.refresh") }}</el-button>
        </div>
      </template>
      <div class="node-monitor-grid">
        <div v-for="node in hardwareRows" :key="node.id" class="node-monitor-card">
          <div class="node-monitor-header">
            <span class="node-name">{{ node.hostname }}</span>
            <span v-if="node.cuda_driver_api_version" class="cuda-badge">CUDA {{ node.cuda_driver_api_version }}</span>
            <el-tag :type="node.status === 'online' ? 'success' : 'danger'" size="small" round>{{ nodeUptimeText(node) }}</el-tag>
          </div>

          <div class="metric-section metric-section-cpu">
            <div class="metric-section-title">{{ t("dashboard.processor") }}</div>
            <div class="metric-row">
              <div class="metric-label">
                <span>CPU</span>
                <span class="metric-value" :style="{ color: sparklineColor(node.cpu_usage_percent ?? 0) }">
                  {{ (node.cpu_usage_percent ?? 0).toFixed(0) }}%
                </span>
              </div>
              <div class="bar-wrap">
                <div class="bar-track">
                  <div class="bar-fill" :style="{ width: (node.cpu_usage_percent ?? 0) + '%', background: sparklineColor(node.cpu_usage_percent ?? 0) }" />
                </div>
                <span v-if="node.cpu_temperature_c > 0" class="bar-pct" style="color:var(--el-text-color-secondary)">{{ node.cpu_temperature_c }}°C</span>
              </div>
            </div>

            <div v-if="node.cpu_model || node.cpu_total" class="metric-row cpu-info-row">
              <!-- 第一行：CPU 型号 -->
              <div class="cpu-model-text">
                {{ formatCpuHardware(node)[0] }}
              </div>

              <!-- 第二行：核心与线程（仅在有第二行时渲染） -->
              <div v-if="formatCpuHardware(node)[1]" class="cpu-detail-text">
                {{ formatCpuHardware(node)[1] }}
              </div>
            </div>
          </div>

          <div class="metric-section metric-section-container">
            <div class="metric-section-title">{{ t("dashboard.containers") }}</div>
            <div class="metric-row compact-row">
              <div class="metric-label">
                <span>{{ t("dashboard.runningContainers") }}</span>
                <span class="metric-value">{{ t("dashboard.containerRunning", { running: node.containers_running ?? 0, total: node.containers_total }) }}</span>
              </div>
            </div>
          </div>

          <div class="metric-section metric-section-memory">
            <div class="metric-section-title">{{ t("dashboard.memorySwap") }}</div>
            <div class="metric-row">
              <div class="metric-label">
                <span>{{ t("dashboard.memory") }}</span>
                <span class="metric-value">{{ formatGbPair(node.memory_used_gb, node.memory_total_gb) }}</span>
              </div>
              <div class="bar-wrap">
                <div class="bar-track">
                  <div class="bar-fill" :style="{ width: pct(node.memory_used_gb, node.memory_total_gb) + '%', background: pctColor(pct(node.memory_used_gb, node.memory_total_gb)) }" />
                </div>
                <span class="bar-pct">{{ pct(node.memory_used_gb, node.memory_total_gb) }}%</span>
              </div>
            </div>
            <div v-if="node.swap_total_gb > 0" class="metric-row">
              <div class="metric-label">
                <span>Swap</span>
                <span class="metric-value">{{ formatGbPair(node.swap_used_gb, node.swap_total_gb) }}</span>
              </div>
              <div class="bar-wrap">
                <div class="bar-track">
                  <div class="bar-fill" :style="{ width: pct(node.swap_used_gb, node.swap_total_gb) + '%', background: pctColor(pct(node.swap_used_gb, node.swap_total_gb)) }" />
                </div>
                <span class="bar-pct">{{ pct(node.swap_used_gb, node.swap_total_gb) }}%</span>
              </div>
            </div>
          </div>

          <div class="metric-section metric-section-disk">
            <div class="metric-section-title">{{ t("dashboard.disk") }}</div>
            <div class="metric-row">
              <div class="metric-label">
                <span>{{ t("dashboard.storagePool") }}</span>
                <span class="metric-value">{{ formatGbPair(node.disk_used_gb, node.disk_total_gb) }}</span>
              </div>
              <div class="bar-wrap">
                <div class="bar-track">
                  <div class="bar-fill" :style="{ width: pct(node.disk_used_gb, node.disk_total_gb) + '%', background: pctColor(pct(node.disk_used_gb, node.disk_total_gb)) }" />
                </div>
                <span class="bar-pct">{{ pct(node.disk_used_gb, node.disk_total_gb) }}%</span>
              </div>
            </div>
          </div>

          <div v-if="nodeGpus(node).length" class="metric-section metric-section-gpu">
            <div class="metric-section-title">
              <span>{{ t("dashboard.gpu") }}</span>
            </div>
            <template v-for="gpu in nodeGpus(node)" :key="gpu.id">
              <div class="metric-row gpu-metric-row">
                <div class="metric-label">
                  <span class="gpu-label">{{ gpu.model || 'GPU' }} #{{ gpu.slot }}</span>
                  <span class="metric-value">{{ t("dashboard.utilization", { value: metricNumber(gpu.utilization) }) }}</span>
                </div>
                <div class="bar-wrap">
                  <div class="bar-track">
                    <div class="bar-fill" :style="{ width: metricNumber(gpu.utilization) + '%', background: pctColor(metricNumber(gpu.utilization)) }" />
                  </div>
                  <span class="bar-pct">{{ metricNumber(gpu.temperature_c) }}°C</span>
                </div>
              </div>
              <div class="metric-row gpu-memory-row">
                <div class="metric-label">
                  <span class="gpu-label">{{ t("dashboard.vram") }}</span>
                  <span class="metric-value">{{ formatMbPair(gpu.vram_used_mb ?? 0, (gpu.vram_gb ?? 0) * 1024) }}</span>
                </div>
                <div class="bar-wrap">
                  <div class="bar-track">
                    <div class="bar-fill" :style="{ width: pct(gpu.vram_used_mb ?? 0, (gpu.vram_gb ?? 0) * 1024) + '%', background: pctColor(pct(gpu.vram_used_mb ?? 0, (gpu.vram_gb ?? 0) * 1024)) }" />
                  </div>
                  <span class="bar-pct">{{ pct(gpu.vram_used_mb ?? 0, (gpu.vram_gb ?? 0) * 1024) }}%</span>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
        </el-card>
      </el-tab-pane>
      <el-tab-pane v-if="isAdmin" :label="t('nav.monitoring')">
        <MonitoringPage />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.node-monitor-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}

.node-monitor-card {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 14px 16px;
  background: var(--el-bg-color-page);
}

.node-monitor-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.node-name {
  font-weight: 600;
  font-size: 14px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cuda-badge {
  flex: 0 0 auto;
  font-size: 11px;
  color: #fff;
  background: #0891b2;
  border: 1px solid #0891b2;
  padding: 2px 7px;
  border-radius: 999px;
  font-weight: 600;
}

.metric-section {
  position: relative;
  padding: 10px 12px 10px 14px;
  margin-top: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
  box-shadow: inset 3px 0 0 var(--section-accent, var(--el-color-primary));
}

.node-monitor-header + .metric-section {
  margin-top: 0;
}

.metric-section-cpu {
  --section-accent: var(--el-color-primary);
}

.metric-section-container {
  --section-accent: var(--el-color-success);
}

.metric-section-memory {
  --section-accent: var(--el-color-warning);
}

.metric-section-disk {
  --section-accent: #8b5cf6;
}

.metric-section-gpu {
  --section-accent: var(--el-color-danger);
}

.metric-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.metric-section-title::before {
  content: "";
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--section-accent, var(--el-color-primary));
}

.metric-row {
  margin-bottom: 8px;
}

.metric-section .metric-row:last-child {
  margin-bottom: 0;
}

.cpu-info-row {
  display: flex;
  flex-direction: column;
  gap: 2px; /* 两行之间的微小间距 */
}

.cpu-model-text {
  font-weight: 500;
  color: #1f2937; /* 加深型号颜色 */
}

.cpu-detail-text {
  font-size: 12px;
  color: #6b7280; /* 次要信息使用灰字 */
}

.compact-row {
  margin-bottom: 0;
}

.metric-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 3px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.metric-value {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.gpu-label {
  font-size: 11px;
  color: var(--el-color-primary);
}

.cpu-info-row {
  margin-bottom: 0;
}

.cpu-model-text {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}

.sparkline-wrap {
  height: 28px;
  width: 100%;
}

.sparkline {
  width: 100%;
  height: 28px;
  display: block;
  overflow: visible;
}

.bar-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
}

.bar-track {
  flex: 1;
  height: 8px;
  background: var(--el-fill-color);
  border-radius: 4px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.4s ease;
}

.bar-pct {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  min-width: 36px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.gpu-metric-row {
  margin-top: 8px;
}

.metric-section-title + .gpu-metric-row {
  margin-top: 0;
}

.gpu-memory-row {
  padding-left: 12px;
}

.gpu-memory-row .gpu-label {
  color: var(--el-text-color-secondary);
}
</style>
