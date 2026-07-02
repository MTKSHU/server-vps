<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import { getRecentTasks, type RecentTask } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false);
const tasks = ref<RecentTask[]>([]);
const currentPage = ref(1);
const pageSize = 20;

let timer: ReturnType<typeof setInterval> | null = null;

const activeTaskCount = computed(() =>
  tasks.value.filter(r => ["pending", "claimed", "planned", "running", "verifying"].includes(r.status)).length
);

const pagedTasks = computed(() => {
  const start = (currentPage.value - 1) * pageSize;
  return tasks.value.slice(start, start + pageSize);
});

async function load() {
  loading.value = true;
  try {
    tasks.value = await getRecentTasks();
    // 刷新时若当前页超出范围则回到第一页
    const maxPage = Math.max(1, Math.ceil(tasks.value.length / pageSize));
    if (currentPage.value > maxPage) currentPage.value = 1;
  } finally { loading.value = false; }
}

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
  timer = setInterval(() => { if (activeTaskCount.value > 0) load(); }, 5000);
});
onUnmounted(() => { if (timer) clearInterval(timer); });
</script>

<template>
  <div class="page-stack">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <strong>{{ t("nav.tasks") }}</strong>
          <el-button :icon="Refresh" :loading="loading" size="small" @click="load">{{ t("common.refresh") }}</el-button>
        </div>
      </template>

      <el-alert v-if="activeTaskCount > 0" type="info" :closable="false" style="margin-bottom:12px">
        {{ activeTaskCount }} 个任务正在进行，每 5 秒自动刷新
      </el-alert>

      <el-table :data="pagedTasks" stripe size="small" v-loading="loading">
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

      <div v-if="tasks.length > pageSize" style="margin-top:16px;display:flex;justify-content:flex-end">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="tasks.length"
          layout="total, prev, pager, next"
          background
        />
      </div>
      <p v-if="!loading && tasks.length === 0" style="text-align:center;color:var(--el-text-color-placeholder);padding:24px 0;margin:0">暂无任务记录</p>
    </el-card>
  </div>
</template>
