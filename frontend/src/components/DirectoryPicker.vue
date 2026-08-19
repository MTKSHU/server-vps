<script setup lang="ts">
import { computed, ref } from "vue";
import { Refresh, Close, Select, FolderOpened, Folder } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import {
  getUserDirectory,
  getUserDirectoryLive,
  scanUserDirectory,
  getSharedResourceFiles,
  scanSharedResource,
  type UserDirectoryScan,
  type UserDirectoryEntry,
  type SharedResource,
} from "../api/cluster";

const props = defineProps<{
  /** 选择器类型：user_file 浏览用户个人文件，resource 浏览公开资源 */
  pickerType: "user_file" | "resource";
  /** 当 pickerType === 'user_file' 时必传 */
  userId?: number;
  /** 当 pickerType === 'resource' 时必传 */
  resource?: SharedResource | null;
}>();

const emit = defineEmits<{
  (e: "pick", path: string): void;
}>();

const visible = ref(false);
const loading = ref(false);
const currentPath = ref("");
const scan = ref<UserDirectoryScan | null>(null);

const entries = computed<(UserDirectoryEntry & { _virtual?: "parent" })[]>(() => {
  const list = (scan.value?.entries || []) as (UserDirectoryEntry & { _virtual?: "parent" })[];
  if (!currentPath.value) return list;
  return [{ name: "上一级", type: "directory", size_bytes: 0, mtime: 0, mode: "", _virtual: "parent" }, ...list];
});

const directoryEntries = computed(() => entries.value.filter((e) => e.type === "directory"));

const currentDisplayPath = computed(() => currentPath.value || "/");

async function load() {
  if (props.pickerType === "user_file") {
    if (!props.userId) return;
    loading.value = true;
    try {
      // 优先使用即时 SSH ls，毫秒级返回
      const data = await getUserDirectoryLive(props.userId, currentPath.value);
      scan.value = data;
    } catch {
      // 即时 ls 失败时回退到缓存扫描
      try {
        const data = await getUserDirectory(props.userId, currentPath.value);
        scan.value = data;
        if (data.status === "unknown") {
          await triggerScan();
        }
      } catch {
        ElMessage.warning("无法加载目录列表");
      }
    } finally {
      loading.value = false;
    }
  } else {
    if (!props.resource) return;
    loading.value = true;
    try {
      const data = await getSharedResourceFiles(props.resource.id, currentPath.value);
      scan.value = data as any;
      if (data.status === "unknown") {
        await triggerScan();
      }
    } finally {
      loading.value = false;
    }
  }
}

async function triggerScan() {
  try {
    if (props.pickerType === "user_file" && props.userId) {
      await scanUserDirectory(props.userId, currentPath.value);
    } else if (props.resource) {
      await scanSharedResource(props.resource.id, currentPath.value);
    }
    // 轮询等待扫描完成
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      if (props.pickerType === "user_file" && props.userId) {
        const data = await getUserDirectory(props.userId, currentPath.value);
        scan.value = data;
        if (data.status === "ready" || data.status === "failed") break;
      } else if (props.resource) {
        const data = await getSharedResourceFiles(props.resource.id, currentPath.value);
        scan.value = data as any;
        if (data.status === "ready" || data.status === "failed") break;
      }
    }
  } catch {
    ElMessage.warning("目录扫描失败");
  }
}

async function refresh() {
  if (props.pickerType === "user_file" && props.userId) {
    loading.value = true;
    try {
      const data = await getUserDirectoryLive(props.userId, currentPath.value);
      scan.value = data;
    } catch {
      await triggerScan();
    } finally {
      loading.value = false;
    }
  } else {
    await triggerScan();
  }
}

function enterDir(name: string) {
  currentPath.value = currentPath.value ? `${currentPath.value}/${name}` : name;
  load();
}

function upDir() {
  const parts = currentPath.value.split("/");
  parts.pop();
  currentPath.value = parts.join("/");
  load();
}

function handleRowClick(row: UserDirectoryEntry & { _virtual?: string }) {
  if (row._virtual === "parent") {
    upDir();
  } else if (row.type === "directory") {
    enterDir(row.name);
  }
}

function confirmSelect() {
  emit("pick", currentPath.value);
  visible.value = false;
}

function open() {
  currentPath.value = "";
  scan.value = null;
  visible.value = true;
  load();
}

defineExpose({ open });
</script>

<template>
  <el-dialog
    v-model="visible"
    class="directory-picker-dialog"
    :title="pickerType === 'user_file' ? '选择目录 · 我的文件' : `选择目录 · ${resource?.name || '公开资源'}`"
    width="680px"
    destroy-on-close
  >
    <div v-loading="loading">
      <div class="picker-header">
        <div>
          <el-tag type="info" size="small">
            <el-icon><FolderOpened /></el-icon>
            {{ currentDisplayPath }}
          </el-tag>
          <span class="picker-info">
            {{ scan?.file_count ?? '-' }} 个文件，
            {{ scan?.size_bytes ? (scan.size_bytes >= 1073741824 ? (scan.size_bytes / 1073741824).toFixed(1) + ' GB' : scan.size_bytes >= 1048576 ? (scan.size_bytes / 1048576).toFixed(1) + ' MB' : (scan.size_bytes / 1024).toFixed(1) + ' KB') : '0 B' }}
          </span>
        </div>
        <el-button :icon="Refresh" size="small" @click="refresh" :loading="loading">刷新</el-button>
      </div>
      <el-table
        :data="entries"
        stripe
        highlight-current-row
        @row-click="handleRowClick"
        style="cursor: pointer; max-height: 400px; overflow-y: auto"
      >
        <el-table-column label="名称" min-width="280">
          <template #default="{ row }">
            <div class="entry-name">
              <el-icon v-if="row._virtual === 'parent' || row.type === 'directory'" color="#409EFF">
                <FolderOpened />
              </el-icon>
              <el-icon v-else color="#909399"><Folder /></el-icon>
              <span :style="{ color: row.type === 'directory' ? '#409EFF' : '#606266', fontWeight: row.type === 'directory' ? 500 : 400 }">
                {{ row._virtual === 'parent' ? '📂 ..' : row.name }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            {{ row._virtual === 'parent' ? '-' : row.type === 'directory' ? '目录' : '文件' }}
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">
            <template v-if="row._virtual === 'parent'">-</template>
            <template v-else-if="row.type === 'directory'">-</template>
            <template v-else>
              {{ row.size_bytes >= 1073741824 ? (row.size_bytes / 1073741824).toFixed(1) + ' GB' : row.size_bytes >= 1048576 ? (row.size_bytes / 1048576).toFixed(1) + ' MB' : (row.size_bytes / 1024).toFixed(1) + ' KB' }}
            </template>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="directoryEntries.length === 0 && !loading" class="picker-empty">
        当前路径下没有子目录
      </div>
    </div>
    <template #footer>
      <el-button :icon="Close" @click="visible = false">取消</el-button>
      <el-button type="primary" :icon="Select" @click="confirmSelect">确认选中此目录</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.picker-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.picker-info {
  margin-left: 10px;
  font-size: 13px;
  color: #909399;
}
.entry-name {
  display: flex;
  align-items: center;
  gap: 6px;
}
.picker-empty {
  text-align: center;
  padding: 32px 0;
  color: #909399;
  font-size: 14px;
}

:deep(.directory-picker-dialog .el-button > span) {
  display: inline-flex !important;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
</style>
