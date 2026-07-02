<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Close, Delete, Plus, Select, Upload } from "@element-plus/icons-vue";
import { getMe, updateProfile, getSshKeys, addSshKey, deleteSshKey, syncSshKeysToContainers, changePassword, getApiTokens, createApiToken, deleteApiToken, type SshKey, type ApiToken } from "../api/cluster";
import { authUser, setAuth, authToken } from "../auth";

const { locale, t } = useI18n();
const loading = ref(false);
const saving = ref(false);

const info = ref<{
  username: string; display_name: string; email: string; phone: string; group_name: string;
  quota?: { cpu_cores: number; memory_gb: number; disk_gb: number; container_disk_limit_gb: number; storage_quota_gb: number; gpu_count: number; container_count: number };
} | null>(null);

const profileForm = reactive({ display_name: "", email: "", phone: "" });
const groupLabelKeys: Record<string, string> = {
  platform_admin: "users.groupPlatformAdmin",
  admin: "users.groupAdmin",
  member: "users.groupMember",
  guest: "users.groupGuest",
};
function groupLabel(groupName: string) {
  return groupLabelKeys[groupName] ? t(groupLabelKeys[groupName]) : groupName;
}

// Password change
const passwordSaving = ref(false);
const passwordForm = reactive({ current_password: "", new_password: "", confirm_password: "" });

async function submitPasswordChange() {
  if (!passwordForm.current_password || !passwordForm.new_password) {
    ElMessage.error(t("profile.passwordRequired"));
    return;
  }
  if (passwordForm.new_password !== passwordForm.confirm_password) {
    ElMessage.error(t("profile.passwordMismatch"));
    return;
  }
  passwordSaving.value = true;
  try {
    await changePassword({ current_password: passwordForm.current_password, new_password: passwordForm.new_password });
    ElMessage.success(t("profile.passwordChanged"));
    Object.assign(passwordForm, { current_password: "", new_password: "", confirm_password: "" });
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || t("profile.passwordChangeFailed"));
  } finally {
    passwordSaving.value = false;
  }
}

// SSH Keys
const sshKeys = ref<SshKey[]>([]);
const keyDialogVisible = ref(false);
const keyAdding = ref(false);
const keySyncing = ref(false);
const keyForm = reactive({ label: "", public_key: "", expires_at: null as Date | null });

// API Tokens
const apiTokens = ref<ApiToken[]>([]);
const tokenDialogVisible = ref(false);
const tokenCreating = ref(false);
const tokenForm = reactive({ name: "", expires_days: 0 });
const newTokenValue = ref("");

async function loadApiTokens() {
  apiTokens.value = await getApiTokens();
}

async function submitCreateToken() {
  tokenCreating.value = true;
  try {
    const expires_at = tokenForm.expires_days > 0
      ? Math.floor(Date.now() / 1000) + tokenForm.expires_days * 86400
      : 0;
    const result = await createApiToken({ name: tokenForm.name, expires_at });
    newTokenValue.value = result.token;
    apiTokens.value = await getApiTokens();
    tokenForm.name = "";
    tokenForm.expires_days = 0;
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : "创建失败"); }
  finally { tokenCreating.value = false; }
}

async function removeToken(token: ApiToken) {
  await ElMessageBox.confirm(`确认吊销 Token「${token.name || token.token_preview}」？`, "吊销 API Token", { type: "warning" });
  await deleteApiToken(token.id);
  apiTokens.value = apiTokens.value.filter(t => t.id !== token.id);
  ElMessage.success("Token 已吊销");
}

function copyToken() {
  navigator.clipboard.writeText(newTokenValue.value).then(() => ElMessage.success("已复制到剪贴板"));
}

async function load() {
  loading.value = true;
  try {
    const [data, keys] = await Promise.all([getMe(), getSshKeys()]);
    info.value = data as typeof info.value;
    profileForm.display_name = data.display_name;
    profileForm.email = data.email ?? "";
    profileForm.phone = data.phone ?? "";
    sshKeys.value = keys;
    await loadApiTokens();
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
    ElMessage.success(t("profile.saved"));
    if (authUser.value) {
      setAuth(authToken.value, { ...authUser.value, display_name: updated.display_name });
    }
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || t("profile.saveFailed"));
  } finally {
    saving.value = false;
  }
}

function openAddKey() {
  Object.assign(keyForm, { label: "", public_key: "", expires_at: null });
  keyDialogVisible.value = true;
}

async function submitAddKey() {
  if (!keyForm.public_key.trim()) { ElMessage.error(t("profile.keyRequired")); return; }
  keyAdding.value = true;
  try {
    const expiresAt = keyForm.expires_at ? Math.floor(keyForm.expires_at.getTime() / 1000) : 0;
    await addSshKey({ label: keyForm.label, public_key: keyForm.public_key.trim(), expires_at: expiresAt });
    ElMessage.success(t("profile.keyAdded"));
    keyDialogVisible.value = false;
    sshKeys.value = await getSshKeys();
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || t("profile.addFailed"));
  } finally {
    keyAdding.value = false;
  }
}

async function removeKey(key: SshKey) {
  try {
    await ElMessageBox.confirm(
      t("profile.confirmDeleteKey", { name: key.label || key.public_key.slice(0, 30) + "…" }),
      t("profile.confirmDeleteTitle"),
      { type: "warning", confirmButtonText: t("profile.delete"), cancelButtonText: t("common.cancel") }
    );
  } catch { return; }
  try {
    await deleteSshKey(key.id);
    ElMessage.success(t("profile.deleted"));
    sshKeys.value = sshKeys.value.filter(k => k.id !== key.id);
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || t("profile.deleteFailed"));
  }
}

function formatExpiry(ts: number) {
  if (!ts) return t("profile.neverExpires");
  const d = new Date(ts * 1000);
  const now = Date.now();
  if (d.getTime() < now) return t("profile.expired");
  return d.toLocaleDateString(locale.value);
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
    ElMessage.success(t("profile.syncSuccess", { containers: result.container_count, tasks: result.task_ids.length }));
  } catch (e: unknown) {
    ElMessage.error((e as { detail?: string })?.detail || t("profile.syncFailed"));
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
      <template #header><strong>{{ t("profile.basicInfo") }}</strong></template>
      <el-form v-if="info" :model="profileForm" label-position="top" class="form-grid">
        <el-form-item :label="t('profile.username')">
          <el-input :value="info.username" disabled />
        </el-form-item>
        <el-form-item :label="t('profile.group')">
          <el-input :value="groupLabel(info.group_name)" disabled />
        </el-form-item>
        <el-form-item :label="t('profile.displayName')">
          <el-input v-model="profileForm.display_name" :placeholder="t('profile.displayNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('profile.email')">
          <el-input :value="profileForm.email || t('profile.unset')" disabled />
        </el-form-item>
        <el-form-item :label="t('profile.phone')">
          <el-input v-model="profileForm.phone" :placeholder="t('profile.phonePlaceholder')" />
        </el-form-item>
      </el-form>
      <div style="margin-top:16px;text-align:right">
        <el-button type="primary" :icon="Select" :loading="saving" @click="submitProfile">{{ t("common.save") }}</el-button>
      </div>
    </el-card>

    <!-- 修改密码 -->
    <el-card shadow="never">
      <template #header><strong>{{ t("profile.changePassword") }}</strong></template>
      <el-form :model="passwordForm" label-position="top" class="form-grid">
        <el-form-item :label="t('profile.currentPassword')">
          <el-input v-model="passwordForm.current_password" type="password" show-password :placeholder="t('profile.currentPassword')" />
        </el-form-item>
        <el-form-item :label="t('profile.newPassword')">
          <el-input v-model="passwordForm.new_password" type="password" show-password :placeholder="t('profile.newPassword')" />
        </el-form-item>
        <el-form-item :label="t('profile.confirmPassword')">
          <el-input v-model="passwordForm.confirm_password" type="password" show-password :placeholder="t('profile.confirmPassword')" />
        </el-form-item>
      </el-form>
      <div style="margin-top:16px;text-align:right">
        <el-button type="primary" :icon="Select" :loading="passwordSaving" @click="submitPasswordChange">{{ t("common.save") }}</el-button>
      </div>
    </el-card>

    <!-- SSH 公钥管理 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <strong>{{ t("profile.sshKeys") }}</strong>
          <div style="display:flex;gap:8px">
            <el-button size="small" :icon="Upload" :loading="keySyncing" @click="syncKeysToContainers">{{ t("profile.syncToContainers") }}</el-button>
            <el-button type="primary" size="small" :icon="Plus" @click="openAddKey">{{ t("profile.addKey") }}</el-button>
          </div>
        </div>
      </template>
      <el-table :data="sshKeys" stripe style="width:100%">
        <el-table-column :label="t('profile.label')" min-width="120">
          <template #default="{row}">{{ row.label || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('profile.publicKey')" min-width="260">
          <template #default="{row}">
            <code style="font-size:12px;word-break:break-all">{{ keyPreview(row.public_key) }}</code>
          </template>
        </el-table-column>
        <el-table-column :label="t('profile.expiry')" width="120">
          <template #default="{row}">
            <el-tag :type="row.expires_at && row.expires_at * 1000 < Date.now() ? 'danger' : 'success'" size="small">
              {{ formatExpiry(row.expires_at) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('profile.actions')" width="80">
          <template #default="{row}">
            <el-button size="small" type="danger" text :icon="Delete" @click="removeKey(row)">{{ t("profile.delete") }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="sshKeys.length === 0" :description="t('profile.noKeys')" />
    </el-card>

    <!-- 资源配额 -->
    <el-card v-if="info?.quota" shadow="never">
      <template #header><strong>{{ t("profile.resourceQuota") }}</strong></template>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="CPU">{{ t("profile.cores", { count: info.quota.cpu_cores }) }}</el-descriptions-item>
        <el-descriptions-item :label="t('profile.memory')">{{ info.quota.memory_gb }} GB</el-descriptions-item>
        <el-descriptions-item :label="t('profile.diskQuota')">{{ info.quota.disk_gb }} GB</el-descriptions-item>
        <el-descriptions-item :label="t('profile.rootDiskLimit')">{{ info.quota.container_disk_limit_gb ? `${info.quota.container_disk_limit_gb} GB` : t("profile.unlimited") }}</el-descriptions-item>
        <el-descriptions-item :label="t('profile.storageQuota')">{{ info.quota.storage_quota_gb ? `${info.quota.storage_quota_gb} GB` : t("profile.unlimited") }}</el-descriptions-item>
        <el-descriptions-item label="GPU">{{ info.quota.gpu_count }}</el-descriptions-item>
        <el-descriptions-item :label="t('profile.containers')">{{ info.quota.container_count }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 添加公钥弹窗 -->
    <el-dialog v-model="keyDialogVisible" :title="t('profile.addSshKey')" width="600px">
      <el-form :model="keyForm" label-position="top">
        <el-form-item :label="t('profile.labelOptional')">
          <el-input v-model="keyForm.label" :placeholder="t('profile.labelPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('profile.publicKeyContent')">
          <el-input v-model="keyForm.public_key" type="textarea" :rows="4" :placeholder="t('profile.publicKeyPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('profile.expiryOptional')">
          <el-date-picker
            v-model="keyForm.expires_at"
            type="date"
            :placeholder="t('profile.expiryPlaceholder')"
            :disabled-date="(d: Date) => d < new Date()"
            style="width:100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :icon="Close" @click="keyDialogVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :icon="Plus" :loading="keyAdding" @click="submitAddKey">{{ t("profile.addKey") }}</el-button>
      </template>
    </el-dialog>

    <!-- API Token 管理 -->
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <strong>API Token</strong>
          <el-button type="primary" size="small" :icon="Plus" @click="tokenDialogVisible = true; newTokenValue = ''">创建 Token</el-button>
        </div>
      </template>
      <p style="color:var(--el-text-color-secondary);margin:0 0 12px;font-size:13px">
        API Token 可用于脚本自动化调用平台接口，权限与当前账号相同。Token 明文仅在创建时显示一次，请妥善保存。
      </p>
      <el-table :data="apiTokens" size="small" stripe>
        <el-table-column label="名称" prop="name" min-width="140">
          <template #default="{ row }">{{ row.name || "(无名称)" }}</template>
        </el-table-column>
        <el-table-column label="Token 预览" prop="token_preview" width="180" />
        <el-table-column label="到期时间" width="160">
          <template #default="{ row }">{{ row.expires_at ? new Date(row.expires_at * 1000).toLocaleDateString() : "永不过期" }}</template>
        </el-table-column>
        <el-table-column label="最后使用" width="160">
          <template #default="{ row }">{{ row.last_used_at ? new Date(row.last_used_at * 1000).toLocaleString() : "-" }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" align="right">
          <template #default="{ row }">
            <el-button type="danger" size="small" text @click="removeToken(row)">吊销</el-button>
          </template>
        </el-table-column>
      </el-table>
      <p v-if="apiTokens.length === 0" style="color:var(--el-text-color-placeholder);text-align:center;padding:16px 0;margin:0">暂无 API Token</p>
    </el-card>

    <!-- 创建 Token 弹窗 -->
    <el-dialog v-model="tokenDialogVisible" title="创建 API Token" width="500px" @closed="newTokenValue = ''">
      <div v-if="!newTokenValue">
        <el-form label-width="90px">
          <el-form-item label="名称">
            <el-input v-model="tokenForm.name" placeholder="如：我的脚本" maxlength="50" />
          </el-form-item>
          <el-form-item label="有效期">
            <el-select v-model="tokenForm.expires_days" style="width:100%">
              <el-option label="永不过期" :value="0" />
              <el-option label="7 天" :value="7" />
              <el-option label="30 天" :value="30" />
              <el-option label="90 天" :value="90" />
              <el-option label="180 天" :value="180" />
              <el-option label="365 天" :value="365" />
            </el-select>
          </el-form-item>
        </el-form>
      </div>
      <div v-else>
        <el-alert type="warning" :closable="false" style="margin-bottom:12px">
          Token 明文仅显示一次，关闭弹窗后无法再次查看，请立即复制保存。
        </el-alert>
        <el-input :value="newTokenValue" readonly type="textarea" :rows="3" style="font-family:monospace" />
        <div style="margin-top:8px;text-align:right">
          <el-button type="primary" size="small" @click="copyToken">复制</el-button>
        </div>
      </div>
      <template #footer>
        <el-button @click="tokenDialogVisible = false">关闭</el-button>
        <el-button v-if="!newTokenValue" type="primary" :loading="tokenCreating" @click="submitCreateToken">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>
