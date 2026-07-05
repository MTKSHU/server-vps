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
let refreshTimer: ReturnType<typeof setInterval> | null = null;

const hoursOptions = computed(() => [
  { label: t("monitoring.hours1"), value: 1 },
  { label: t("monitoring.hours6"), value: 6 },
  { label: t("monitoring.hours24"), value: 24 },
  { label: t("monitoring.days7"), value: 168 },
]);

const selectedNode = computed(() => nodes.value.find(n => n.id === selectedNodeId.value) ?? null);

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
  try {
    snapshots.value = await getNodeMetricsHistory(selectedNodeId.value, hours.value);
  } finally { histLoading.value = false; }
}

async function onNodeChange() { await loadHistory(); }
async function onHoursChange() { await loadHistory(); }

// SVG 面积图（与 Dashboard 同款算法）
function sparkline(data: number[], w = 400, h = 60) {
  if (data.length < 2) return { line: "", area: "" };
  const step = w / (data.length - 1);
  const pts = data.map((v, i) => `${(i * step).toFixed(1)},${(h - (v / 100) * h).toFixed(1)}`);
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

interface ChartSpec { key: keyof MetricsSnapshot; labelKey: string; unit: string }
const charts: ChartSpec[] = [
  { key: "cpu_pct", labelKey: "monitoring.cpuUsage", unit: "%" },
  { key: "memory_pct", labelKey: "monitoring.memoryUsage", unit: "%" },
  { key: "disk_pct", labelKey: "monitoring.diskUsage", unit: "%" },
  { key: "gpu_avg_pct", labelKey: "monitoring.gpuAvgUsage", unit: "%" },
  { key: "gpu_avg_vram_pct", labelKey: "monitoring.gpuAvgVram", unit: "%" },
];

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
            <el-option v-for="n in nodes" :key="n.id" :label="n.hostname" :value="n.id">
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
            <span class="chart-title">{{ t(chart.labelKey) }}</span>
            <span class="chart-current" :style="{ color: color(Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0)) }">
              {{ snapshots.length ? Number(snapshots[snapshots.length - 1]![chart.key]).toFixed(1) + chart.unit : '-' }}
            </span>
          </div>
          <svg :width="'100%'" height="70" viewBox="0 0 400 60" preserveAspectRatio="none" class="chart-svg">
            <defs>
              <linearGradient :id="`grad-${chart.key}`" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" :stop-color="color(Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))" stop-opacity="0.35" />
                <stop offset="100%" :stop-color="color(Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))" stop-opacity="0.04" />
              </linearGradient>
            </defs>
            <template v-if="snapshots.length >= 2">
              <polygon
                :points="sparkline(snapshots.map(s => Number(s[chart.key]))).area"
                :fill="`url(#grad-${chart.key})`"
              />
              <polyline
                :points="sparkline(snapshots.map(s => Number(s[chart.key]))).line"
                fill="none"
                :stroke="color(Number(snapshots[snapshots.length - 1]?.[chart.key] ?? 0))"
                stroke-width="1.5"
                stroke-linejoin="round"
                stroke-linecap="round"
              />
            </template>
            <text v-else x="200" y="35" text-anchor="middle" font-size="11" fill="var(--el-text-color-placeholder)">{{ t("monitoring.insufficientData") }}</text>
          </svg>
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
.chart-current { font-size: 18px; font-weight: 600; }
.chart-svg { display: block; width: 100%; }
.chart-xticks {
  display: flex;
  justify-content: space-between;
  margin-top: 3px;
}
.xtick { font-size: 10px; color: var(--el-text-color-placeholder); }
</style>
