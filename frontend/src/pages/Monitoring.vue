<script setup lang="ts">
import { onMounted, onUnmounted, ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import { getNodeHardware, getNodeMetricsHistory, type NodeHardware, type MetricsSnapshot } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false);
const nodes = ref<NodeHardware[]>([]);
const selectedNodeId = ref<number | null>(null);
const hours = ref(6);
const snapshots = ref<MetricsSnapshot[]>([]);
const histLoading = ref(false);
const chartHover = ref<{ key: ChartKey; index: number } | null>(null);
let refreshTimer: ReturnType<typeof setInterval> | null = null;

const hoursOptions = computed(() => [
  { label: t("monitoring.hours1"), value: 1 },
  { label: t("monitoring.hours6"), value: 6 },
  { label: t("monitoring.hours24"), value: 24 },
  { label: t("monitoring.days7"), value: 168 },
]);

const selectedNode = computed(() => nodes.value.find(n => n.id === selectedNodeId.value) ?? null);
const orderedNodes = computed(() => [...nodes.value].sort((a, b) =>
  (a.display_order ?? Number.MAX_SAFE_INTEGER) - (b.display_order ?? Number.MAX_SAFE_INTEGER)
  || a.hostname.localeCompare(b.hostname)
));

async function loadNodes() {
  loading.value = true;
  try {
    nodes.value = await getNodeHardware();
    if (!selectedNodeId.value && nodes.value.length) {
      selectedNodeId.value = nodes.value[0].id;
    }
    if (selectedNodeId.value) await loadHistory();
  } finally { loading.value = false; }
}

async function loadHistory() {
  if (!selectedNodeId.value) return;
  histLoading.value = true;
  chartHover.value = null;
  try {
    snapshots.value = await getNodeMetricsHistory(selectedNodeId.value, hours.value);
  } finally { histLoading.value = false; }
}

async function onNodeChange() { await loadHistory(); }
async function onHoursChange() { await loadHistory(); }

// SVG 面积图（与 Dashboard 同款算法）
function sparkline(data: number[], maxValue = 100, w = 400, h = 60) {
  if (data.length < 2) return { line: "", area: "" };
  const step = w / (data.length - 1);
  const scale = Math.max(1, maxValue);
  const pts = data.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / scale) * h).toFixed(1)}`);
  return {
    line: pts.join(" "),
    area: [...pts, `${w.toFixed(1)},${h}`, `0,${h}`].join(" "),
  };
}

function color(lastPct: number) {
  if (lastPct < 60) return "var(--el-color-success)";
  if (lastPct < 85) return "var(--el-color-warning)";
  return "var(--el-color-danger)";
}

function formatTs(ts: number) {
  const d = new Date(ts * 1000);
  if (hours.value <= 6) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (hours.value <= 24) return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function xTicks(data: MetricsSnapshot[], count = 5) {
  if (!data.length) return [];
  const step = Math.max(1, Math.floor(data.length / count));
  return data.filter((_, i) => i % step === 0 || i === data.length - 1);
}

type ChartKey = "cpu_pct" | "memory_pct" | "disk_pct" | "gpu_avg_pct" | "gpu_avg_vram_pct" | "network_rx_bytes_per_sec" | "network_tx_bytes_per_sec";
interface ChartSpec { key: ChartKey; labelKey: string; kind: "percent" | "rate" }
const charts: ChartSpec[] = [
  { key: "cpu_pct", labelKey: "monitoring.cpuUsage", kind: "percent" },
  { key: "memory_pct", labelKey: "monitoring.memoryUsage", kind: "percent" },
  { key: "disk_pct", labelKey: "monitoring.diskUsage", kind: "percent" },
  { key: "gpu_avg_pct", labelKey: "monitoring.gpuAvgUsage", kind: "percent" },
  { key: "gpu_avg_vram_pct", labelKey: "monitoring.gpuAvgVram", kind: "percent" },
  { key: "network_rx_bytes_per_sec", labelKey: "monitoring.networkReceive", kind: "rate" },
  { key: "network_tx_bytes_per_sec", labelKey: "monitoring.networkTransmit", kind: "rate" },
];

const latestNetworkInterface = computed(() => {
  for (let index = snapshots.value.length - 1; index >= 0; index -= 1) {
    const interfaceName = snapshots.value[index]?.network_interface;
    if (interfaceName) return interfaceName;
  }
  return "";
});

function chartValues(chart: ChartSpec) {
  return snapshots.value.map(snapshot => Math.max(0, Number(snapshot[chart.key] ?? 0)));
}

function chartMax(chart: ChartSpec) {
  return chart.kind === "percent" ? 100 : Math.max(1, ...chartValues(chart));
}

function formatRate(bytesPerSecond: number) {
  const units = ["B/s", "KB/s", "MB/s", "GB/s", "TB/s"];
  let value = Math.max(0, bytesPerSecond || 0);
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 || value >= 100 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatChartValue(chart: ChartSpec, value: number) {
  return chart.kind === "rate" ? formatRate(value) : `${value.toFixed(1)}%`;
}

function chartColor(chart: ChartSpec, value: number) {
  if (chart.key === "network_rx_bytes_per_sec") return "var(--el-color-success)";
  if (chart.key === "network_tx_bytes_per_sec") return "var(--el-color-primary)";
  return color(value);
}

function setChartHover(chart: ChartSpec, event: PointerEvent) {
  if (snapshots.value.length < 2) return;
  const target = event.currentTarget as SVGElement;
  const bounds = target.getBoundingClientRect();
  if (!bounds.width) return;
  const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
  chartHover.value = {
    key: chart.key,
    index: Math.round(ratio * (snapshots.value.length - 1)),
  };
}

function focusChart(chart: ChartSpec) {
  if (snapshots.value.length >= 2) {
    chartHover.value = { key: chart.key, index: snapshots.value.length - 1 };
  }
}

function moveChartHover(chart: ChartSpec, offset: number) {
  if (snapshots.value.length < 2) return;
  const current = chartHover.value?.key === chart.key
    ? chartHover.value.index
    : snapshots.value.length - 1;
  chartHover.value = {
    key: chart.key,
    index: Math.min(snapshots.value.length - 1, Math.max(0, current + offset)),
  };
}

function jumpChartHover(chart: ChartSpec, index: number) {
  if (snapshots.value.length < 2) return;
  chartHover.value = {
    key: chart.key,
    index: Math.min(snapshots.value.length - 1, Math.max(0, index)),
  };
}

function clearChartHover(chart: ChartSpec) {
  if (chartHover.value?.key === chart.key) chartHover.value = null;
}

function hoverPoint(chart: ChartSpec) {
  if (chartHover.value?.key !== chart.key || snapshots.value.length < 2) return null;
  const index = Math.min(snapshots.value.length - 1, Math.max(0, chartHover.value.index));
  const snapshot = snapshots.value[index];
  if (!snapshot) return null;
  const value = Math.max(0, Number(snapshot[chart.key] ?? 0));
  const ratio = index / (snapshots.value.length - 1);
  const yRatio = Math.min(1, Math.max(0, value / chartMax(chart)));
  return {
    index,
    snapshot,
    value,
    x: ratio * 400,
    y: (1 - yRatio) * 60,
    left: `${ratio * 100}%`,
    align: ratio < 0.2 ? "start" : ratio > 0.8 ? "end" : "center",
  };
}

function formatHoverTs(ts: number) {
  return new Date(ts * 1000).toLocaleString([], {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

onMounted(() => {
  loadNodes();
  refreshTimer = setInterval(loadHistory, 60000); // 每分钟刷新历史
});
onUnmounted(() => { if (refreshTimer) clearInterval(refreshTimer); });
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <strong>{{ t("monitoring.title") }}</strong>
          <el-select v-model="selectedNodeId" size="small" style="width:180px" @change="onNodeChange">
            <el-option v-for="n in orderedNodes" :key="n.id" :label="n.hostname" :value="n.id">
              <span>{{ n.hostname }}</span>
              <el-tag :type="n.status === 'online' ? 'success' : 'danger'" size="small" style="margin-left:8px">{{ n.status }}</el-tag>
            </el-option>
          </el-select>
          <el-select v-model="hours" size="small" style="width:110px" @change="onHoursChange">
            <el-option v-for="o in hoursOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-button :icon="Refresh" :loading="histLoading || loading" size="small" @click="loadHistory">{{ t("common.refresh") }}</el-button>
          <span v-if="snapshots.length" style="font-size:12px;color:var(--el-text-color-secondary)">
            {{ t("monitoring.samplePoints", { count: snapshots.length }) }}
          </span>
        </div>
      </template>

      <div v-if="!snapshots.length && !histLoading" style="text-align:center;padding:40px;color:var(--el-text-color-placeholder)">
        <p>{{ t("monitoring.noData") }}</p>
        <p v-if="selectedNode?.status !== 'online'" style="color:var(--el-color-warning)">{{ t("monitoring.offlineHint") }}</p>
      </div>

      <div v-loading="histLoading" class="charts-grid">
        <div v-for="chart in charts" :key="chart.key" class="chart-card">
          <div class="chart-header">
            <span class="chart-title">
              {{ t(chart.labelKey) }}
              <small v-if="chart.kind === 'rate' && latestNetworkInterface">{{ latestNetworkInterface }}</small>
            </span>
            <span class="chart-current" :style="{ color: chartColor(chart, Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0)) }">
              {{ snapshots.length ? formatChartValue(chart, Number(snapshots[snapshots.length - 1]![chart.key] ?? 0)) : '-' }}
            </span>
          </div>
          <div class="chart-plot">
            <svg
              :width="'100%'"
              height="70"
              viewBox="0 0 400 60"
              preserveAspectRatio="none"
              class="chart-svg"
              tabindex="0"
              role="img"
              :aria-label="`${t(chart.labelKey)}，${t('monitoring.hoverHint')}`"
              @pointermove="setChartHover(chart, $event)"
              @pointerdown="setChartHover(chart, $event)"
              @pointerleave="clearChartHover(chart)"
              @focus="focusChart(chart)"
              @blur="clearChartHover(chart)"
              @keydown.left.prevent="moveChartHover(chart, -1)"
              @keydown.right.prevent="moveChartHover(chart, 1)"
              @keydown.home.prevent="jumpChartHover(chart, 0)"
              @keydown.end.prevent="jumpChartHover(chart, snapshots.length - 1)"
            >
              <defs>
                <linearGradient :id="`grad-${chart.key}`" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" :stop-color="chartColor(chart, Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))" stop-opacity="0.35" />
                  <stop offset="100%" :stop-color="chartColor(chart, Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))" stop-opacity="0.04" />
                </linearGradient>
              </defs>
              <template v-if="snapshots.length >= 2">
                <polygon
                  :points="sparkline(chartValues(chart), chartMax(chart)).area"
                  :fill="`url(#grad-${chart.key})`"
                />
                <polyline
                  :points="sparkline(chartValues(chart), chartMax(chart)).line"
                  fill="none"
                  :stroke="chartColor(chart, Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))"
                  stroke-width="1.5"
                  stroke-linejoin="round"
                  stroke-linecap="round"
                />
                <template v-if="hoverPoint(chart)">
                  <line
                    :x1="hoverPoint(chart)!.x"
                    y1="0"
                    :x2="hoverPoint(chart)!.x"
                    y2="60"
                    stroke="var(--el-text-color-secondary)"
                    stroke-width="1"
                    stroke-dasharray="3 3"
                    vector-effect="non-scaling-stroke"
                  />
                  <circle
                    :cx="hoverPoint(chart)!.x"
                    :cy="hoverPoint(chart)!.y"
                    r="3.5"
                    :fill="chartColor(chart, hoverPoint(chart)!.value)"
                    stroke="var(--el-bg-color)"
                    stroke-width="2"
                    vector-effect="non-scaling-stroke"
                  />
                </template>
              </template>
              <text v-else x="200" y="35" text-anchor="middle" font-size="11" fill="var(--el-text-color-placeholder)">{{ t("monitoring.insufficientData") }}</text>
            </svg>
            <div
              v-if="hoverPoint(chart)"
              :class="['chart-tooltip', `chart-tooltip--${hoverPoint(chart)!.align}`]"
              :style="{ left: hoverPoint(chart)!.left }"
            >
              <strong :style="{ color: chartColor(chart, hoverPoint(chart)!.value) }">
                {{ formatChartValue(chart, hoverPoint(chart)!.value) }}
              </strong>
              <span>{{ formatHoverTs(hoverPoint(chart)!.snapshot.sampled_at) }}</span>
            </div>
          </div>
          <!-- X 轴时间标签 -->
          <div v-if="snapshots.length >= 2" class="chart-xticks">
            <span v-for="s in xTicks(snapshots)" :key="s.sampled_at" class="xtick">{{ formatTs(s.sampled_at) }}</span>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.charts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
  padding: 4px 0;
}
.chart-card {
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
  padding: 12px;
  background: var(--el-fill-color-blank);
}
.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.chart-title { font-size: 13px; color: var(--el-text-color-secondary); }
.chart-title small { margin-left: 6px; color: var(--el-text-color-placeholder); font-family: monospace; }
.chart-current { font-size: 18px; font-weight: 600; }
.chart-plot {
  position: relative;
}
.chart-svg {
  display: block;
  width: 100%;
  cursor: crosshair;
  border-radius: 4px;
}
.chart-svg:focus-visible {
  outline: 2px solid var(--el-color-primary-light-5);
  outline-offset: 2px;
}
.chart-tooltip {
  position: absolute;
  top: 3px;
  z-index: 2;
  display: grid;
  gap: 2px;
  min-width: 116px;
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-bg-color-overlay);
  box-shadow: var(--el-box-shadow-light);
  pointer-events: none;
  transform: translateX(-50%);
  text-align: center;
  white-space: nowrap;
}
.chart-tooltip strong { font-size: 13px; }
.chart-tooltip span { font-size: 10px; color: var(--el-text-color-secondary); }
.chart-tooltip--start { transform: none; }
.chart-tooltip--end { transform: translateX(-100%); }
.chart-xticks {
  display: flex;
  justify-content: space-between;
  margin-top: 3px;
}
.xtick { font-size: 10px; color: var(--el-text-color-placeholder); }
</style>
