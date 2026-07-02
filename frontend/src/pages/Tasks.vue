<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import { getRecentTasks, type RecentTask } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false);
const items = ref<RecentTask[]>([]);
const total = ref(0);
const currentPage = ref(1);
const pageSize = 20;

let timer: ReturnType<typeof setInterval> | null = null;

const activeCount = computed(() =>
  items.value.filter(r => ["pending", "claimed", "planned", "running", "verifying"].includes(r.status)).length
);

async function load() {
  loading.value = true;
  try {
    const res = await getRecentTasks(currentPage.value, pageSize);
    items.value = res.items;
    total.value = res.total;
  } finally { loading.value = false; }
}

// 切页时重新拉取
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
  if (kind === "sync_task") return `同步 · ${type === "user_home_sync" ? "家目录" : "共享资源"}`;
  const map: Record<string, string> = {
    incus_create_container: "创建容器",
    incus_start_container: "启动容器",
    incus_stop_container: "停止容器",
    incus_restart_container: "重启容器",
    incus_delete_container: "删除容器",
    incus_sync_ssh_keys: "同步 SSH 密钥",
    incus_sync_ports: "同步端口",
    container_data_sync: "数据同步",
    scan_user_directory: "扫描目录",
    scan_shared_resource: "扫描共享资源",
    verify_shared_resource: "校验共享资源",
    ensure_user_zfs_dataset: "初始化存储 Dataset",
    remove_user_zfs_dataset: "移除存储 Dataset",
  };
  return map[type] || type;
}

onMounted(() => {
  load();
  // 仅在第一页有进行中任务时自动刷新（避免跨页切换时意外跳回）
  timer = setInterval(() => { if (currentPage.value === 1 && activeCount.value > 0) load(); }, 5000);
});
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <strong>{{ t("nav.tasks") }} <span v-if="total" style="font-weight:400;font-size:13px;color:var(--el-text-color-secondary)">（共 {{ total }} 条）</span></strong>
          <el-button :icon="Refresh" :loading="loading" size="small" @click="load">{{ t("common.refresh") }}</el-button>
        </div>
      </template>

      <el-alert v-if="activeCount > 0 && currentPage === 1" type="info" :closable="false" style="margin-bottom:12px">
        {{ activeCount }} 个任务正在进行，每 5 秒自动刷新
      </el-alert>

      <el-table :data="items" stripe size="small" v-loading="loading">
        <el-table-column label="任务类型" min-width="160">
          <template #default="{ row }">{{ kindLabel(row.kind, row.type) }}</template>
        </el-table-column>
        <el-table-column label="容器" prop="container_name" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.container_name || "-" }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误" prop="error" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.error || "-" }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="168">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="完成时间" width="168">
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
      <p v-if="!loading && items.length === 0" style="text-align:center;color:var(--el-text-color-placeholder);padding:24px 0;margin:0">暂无任务记录</p>
    </el-card>
  </div>
</template>
