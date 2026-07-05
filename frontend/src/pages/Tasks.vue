<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import { getRecentTasks, type RecentTask } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false);
const items = ref<RecentTask[]>([]);
const total = ref(0);
const currentPage = ref(1);
const pageSize = 20;
const statusGroup = ref("");   // "" | "active" | "failed" | "succeeded"

const STATUS_OPTIONS = [
  { key: "taskCenter.statusAll", value: "", type: "" },
  { key: "taskCenter.statusActive", value: "active", type: "warning" },
  { key: "taskCenter.statusFailed", value: "failed", type: "danger" },
  { key: "taskCenter.statusSucceeded", value: "succeeded", type: "success" },
] as const;

let timer: ReturnType<typeof setInterval> | null = null;

async function load() {
  loading.value = true;
  try {
    const res = await getRecentTasks(currentPage.value, pageSize, statusGroup.value);
    items.value = res.items;
    total.value = res.total;
  } finally { loading.value = false; }
}

function onFilterChange(val: string) {
  statusGroup.value = val;
  currentPage.value = 1;
  load();
}

watch(currentPage, load);

function formatTime(ts: number) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}

function statusType(status: string) {
  if (["succeeded", "ready"].includes(status)) return "success";
  if (["failed"].includes(status)) return "danger";
  if (["pending", "claimed", "planned", "running", "verifying"].includes(status)) return "warning";
  return "info";
}

function kindLabel(kind: string, type: string) {
  if (kind === "sync_task") {
    return type === "user_home_sync"
      ? t("taskCenter.kindSyncUserHome")
      : t("taskCenter.kindSyncShared");
  }
  const map: Record<string, string> = {
    incus_create_container: "taskCenter.kindCreateContainer",
    incus_start_container: "taskCenter.kindStartContainer",
    incus_stop_container: "taskCenter.kindStopContainer",
    incus_restart_container: "taskCenter.kindRestartContainer",
    incus_delete_container: "taskCenter.kindDeleteContainer",
    incus_sync_ssh_keys: "taskCenter.kindSyncSshKeys",
    incus_sync_ports: "taskCenter.kindSyncPorts",
    container_data_sync: "taskCenter.kindDataSync",
    scan_user_directory: "taskCenter.kindScanDirectory",
    scan_shared_resource: "taskCenter.kindScanSharedResource",
    verify_shared_resource: "taskCenter.kindVerifySharedResource",
    ensure_user_zfs_dataset: "taskCenter.kindEnsureDataset",
    remove_user_zfs_dataset: "taskCenter.kindRemoveDataset",
  };
  return map[type] ? t(map[type]) : type;
}

const activeCount = ref(0);

onMounted(() => {
  load();
  timer = setInterval(async () => {
    if (currentPage.value === 1 || statusGroup.value === "active") {
      // 有进行中任务时才自动刷新；通过不带筛选单独查一下数量
      const snap = await getRecentTasks(1, 1, "active").catch(() => ({ total: 0, items: [] }));
      activeCount.value = snap.total;
      if (snap.total > 0) load();
    }
  }, 5000);
});
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <strong>{{ t("nav.tasks") }}</strong>
          <span v-if="total" style="font-size:13px;color:var(--el-text-color-secondary)">{{ t("taskCenter.total", { total }) }}</span>
          <!-- 状态筛选 -->
          <el-button-group size="small" style="margin-left:4px">
            <el-button
              v-for="opt in STATUS_OPTIONS"
              :key="opt.value"
              :type="statusGroup === opt.value ? (opt.type || 'primary') : ''"
              @click="onFilterChange(opt.value)"
            >{{ t(opt.key) }}</el-button>
          </el-button-group>
          <span style="flex:1" />
          <el-button :icon="Refresh" :loading="loading" size="small" @click="load">{{ t("common.refresh") }}</el-button>
        </div>
      </template>

      <el-alert v-if="activeCount > 0" type="info" :closable="false" style="margin-bottom:12px">
        {{ t("taskCenter.activeRefreshing", { count: activeCount }) }}
      </el-alert>

      <el-table :data="items" stripe size="small" v-loading="loading">
        <el-table-column :label="t('taskCenter.taskType')" min-width="160">
          <template #default="{ row }">{{ kindLabel(row.kind, row.type) }}</template>
        </el-table-column>
        <el-table-column :label="t('taskCenter.container')" prop="container_name" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.container_name || "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('taskCenter.status')" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('taskCenter.error')" prop="error" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.error || "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('taskCenter.createdAt')" width="168">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('taskCenter.finishedAt')" width="168">
          <template #default="{ row }">{{ row.finished_at ? formatTime(row.finished_at) : "-" }}</template>
        </el-table-column>
      </el-table>

      <div v-if="total > pageSize" style="margin-top:16px;display:flex;justify-content:flex-end">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          background
        />
      </div>
      <p v-if="!loading && items.length === 0" style="text-align:center;color:var(--el-text-color-placeholder);padding:24px 0;margin:0">{{ t("taskCenter.empty") }}</p>
    </el-card>
  </div>
</template>
