<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Close, Delete, Plus, Select, Upload } from "@element-plus/icons-vue";
import { getMe, updateProfile, getSshKeys, addSshKey, deleteSshKey, syncSshKeysToContainers, type SshKey } from "../api/cluster";
import { authUser, setAuth, authToken } from "../auth";

const loading = ref(false);
const saving = ref(false);

const info = ref<{
  username: string; display_name: string; email: string; phone: string; group_name: string;
  quota?: { cpu_cores: number; memory_gb: number; disk_gb: number; container_disk_limit_gb: number; storage_quota_gb: number; gpu_count: number; container_count: number };
} | null>(null);

const profileForm = reactive({ display_name: "", email: "", phone: "" });
const groupLabels: Record<string, string> = {
  platform_admin: "平台管理员",
  admin: "管理员",
  member: "成员",
  guest: "来宾",
};
function groupLabel(groupName: string) {
  return groupLabels[groupName] || groupName;
}

// SSH Keys
const sshKeys = ref<SshKey[]>([]);
const keyDialogVisible = ref(false);
const keyAdding = ref(false);
const keySyncing = ref(false);
const keyForm = reactive({ label: "", public_key: "", expires_at: null as Date | null });

async function load() {
  loading.value = true;
  try {
    const [data, keys] = await Promise.all([getMe(), getSshKeys()]);
    info.value = data as typeof info.value;
    profileForm.display_name = data.display_name;
    profileForm.email = data.email ?? "";
    profileForm.phone = data.phone ?? "";
    sshKeys.value = keys;
  } finally {
    loading.value = false;
  }
}

async function submitProfile() {
  saving.value = true;
  try {
    const updated = await updateProfile({
      display_name: profileForm.display_name,
      phone: profileForm.phone,
    });
    ElMessage.success("个人信息已保存");
    if (authUser.value) {
      setAuth(authToken.value, { ...authUser.value, display_name: updated.display_name });
    }
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || "保存失败，请重试");
  } finally {
    saving.value = false;
  }
}

function openAddKey() {
  Object.assign(keyForm, { label: "", public_key: "", expires_at: null });
  keyDialogVisible.value = true;
}

async function submitAddKey() {
  if (!keyForm.public_key.trim()) { ElMessage.error("请输入 SSH 公钥"); return; }
  keyAdding.value = true;
  try {
    const expiresAt = keyForm.expires_at ? Math.floor(keyForm.expires_at.getTime() / 1000) : 0;
    await addSshKey({ label: keyForm.label, public_key: keyForm.public_key.trim(), expires_at: expiresAt });
    ElMessage.success("SSH 公钥已添加");
    keyDialogVisible.value = false;
    sshKeys.value = await getSshKeys();
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || "添加失败，请重试");
  } finally {
    keyAdding.value = false;
  }
}

async function removeKey(key: SshKey) {
  try {
    await ElMessageBox.confirm(`确认删除公钥「${key.label || key.public_key.slice(0, 30) + "…"}」？`, "删除确认", { type: "warning" });
  } catch { return; }
  try {
    await deleteSshKey(key.id);
    ElMessage.success("已删除");
    sshKeys.value = sshKeys.value.filter(k => k.id !== key.id);
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || "删除失败");
  }
}

function formatExpiry(ts: number) {
  if (!ts) return "永久有效";
  const d = new Date(ts * 1000);
  const now = Date.now();
  if (d.getTime() < now) return "已过期";
  return d.toLocaleDateString("zh-CN");
}

function keyPreview(pub: string) {
  const parts = pub.trim().split(/\s+/);
  if (parts.length >= 2) {
    const fingerprint = parts[1];
    return `${parts[0]}  ${fingerprint.slice(0, 12)}…${fingerprint.slice(-8)}`;
  }
  return pub.slice(0, 40) + "…";
}

async function syncKeysToContainers() {
  keySyncing.value = true;
  try {
    const result = await syncSshKeysToContainers();
    ElMessage.success(`已更新 ${result.container_count} 个容器的公钥记录，并下发 ${result.task_ids.length} 个同步任务`);
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || "同步失败，请重试");
  } finally {
    keySyncing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <!-- 基本信息 -->
    <el-card shadow="never">
      <template #header><strong>基本信息</strong></template>
      <el-form v-if="info" :model="profileForm" label-position="top" class="form-grid">
        <el-form-item label="用户名">
          <el-input :value="info.username" disabled />
        </el-form-item>
        <el-form-item label="分组">
          <el-input :value="groupLabel(info.group_name)" disabled />
        </el-form-item>
        <el-form-item label="用户名称">
          <el-input v-model="profileForm.display_name" placeholder="显示名称" />
        </el-form-item>
        <el-form-item label="联系邮箱">
          <el-input :value="profileForm.email || '未设置'" disabled />
        </el-form-item>
        <el-form-item label="联系电话">
          <el-input v-model="profileForm.phone" placeholder="手机或固话" />
        </el-form-item>
      </el-form>
      <div style="margin-top:16px;text-align:right">
        <el-button type="primary" :icon="Select" :loading="saving" @click="submitProfile">保存</el-button>
      </div>
    </el-card>

    <!-- SSH 公钥管理 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <strong>SSH 公钥</strong>
          <div style="display:flex;gap:8px">
            <el-button size="small" :icon="Upload" :loading="keySyncing" @click="syncKeysToContainers">同步到所有容器</el-button>
            <el-button type="primary" size="small" :icon="Plus" @click="openAddKey">添加公钥</el-button>
          </div>
        </div>
      </template>
      <el-table :data="sshKeys" stripe style="width:100%">
        <el-table-column label="标签" min-width="120">
          <template #default="{row}">{{ row.label || '—' }}</template>
        </el-table-column>
        <el-table-column label="公钥" min-width="260">
          <template #default="{row}">
            <code style="font-size:12px;word-break:break-all">{{ keyPreview(row.public_key) }}</code>
          </template>
        </el-table-column>
        <el-table-column label="有效期" width="120">
          <template #default="{row}">
            <el-tag :type="row.expires_at && row.expires_at * 1000 < Date.now() ? 'danger' : 'success'" size="small">
              {{ formatExpiry(row.expires_at) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{row}">
            <el-button size="small" type="danger" text :icon="Delete" @click="removeKey(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="sshKeys.length === 0" description="暂无公钥，创建容器时需要至少一个有效公钥" />
    </el-card>

    <!-- 资源配额 -->
    <el-card v-if="info?.quota" shadow="never">
      <template #header><strong>资源配额</strong></template>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="CPU">{{ info.quota.cpu_cores }} 核</el-descriptions-item>
        <el-descriptions-item label="内存">{{ info.quota.memory_gb }} GB</el-descriptions-item>
        <el-descriptions-item label="容器磁盘总额度">{{ info.quota.disk_gb }} GB</el-descriptions-item>
        <el-descriptions-item label="单容器 Root Disk 上限">{{ info.quota.container_disk_limit_gb ? `${info.quota.container_disk_limit_gb} GB` : '不限' }}</el-descriptions-item>
        <el-descriptions-item label="存储节点用户目录上限">{{ info.quota.storage_quota_gb ? `${info.quota.storage_quota_gb} GB` : '不限' }}</el-descriptions-item>
        <el-descriptions-item label="GPU">{{ info.quota.gpu_count }}</el-descriptions-item>
        <el-descriptions-item label="容器数">{{ info.quota.container_count }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 添加公钥弹窗 -->
    <el-dialog v-model="keyDialogVisible" title="添加 SSH 公钥" width="600px">
      <el-form :model="keyForm" label-position="top">
        <el-form-item label="标签（可选）">
          <el-input v-model="keyForm.label" placeholder="例如：我的笔记本" />
        </el-form-item>
        <el-form-item label="公钥内容">
          <el-input v-model="keyForm.public_key" type="textarea" :rows="4" placeholder="ssh-rsa AAAA… 或 ssh-ed25519 AAAA…" />
        </el-form-item>
        <el-form-item label="有效期（留空则永久有效）">
          <el-date-picker
            v-model="keyForm.expires_at"
            type="date"
            placeholder="选择到期日期"
            :disabled-date="(d: Date) => d < new Date()"
            style="width:100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" :icon="Plus" :loading="keyAdding" @click="submitAddKey">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>
