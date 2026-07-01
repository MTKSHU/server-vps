<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Check, Close, Delete, Edit, Plus, Select, Setting } from "@element-plus/icons-vue";
import { getNodes, getQuotaProfiles, getUsers, saveQuotaProfile, saveUser, approveSsoUser, removeUser, type Node, type QuotaProfile, type User } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false); const users = ref<User[]>([]); const profiles = ref<QuotaProfile[]>([]); const nodes = ref<Node[]>([]);
const userVisible = ref(false); const profileVisible = ref(false); const editingId = ref<number | null>(); const editingProfile = ref<QuotaProfile>();
const userForm = reactive({ username:"",display_name:"",phone:"",email:"",group_name:"member",password:"",ssh_key:"",enabled:true,cpu_cores:16,memory_gb:64,disk_gb:500,container_disk_limit_gb:500,storage_quota_gb:500,gpu_count:1,container_count:2,allowed_node_ids:[] as number[] });
const profileForm = reactive({ role:"member" as "admin"|"member",cpu_cores:16,memory_gb:64,disk_gb:500,container_disk_limit_gb:500,storage_quota_gb:500,gpu_count:1,container_count:2,allowed_node_ids:[] as number[] });

const groupLabelKeys: Record<string, string> = { platform_admin: "users.groupPlatformAdmin", admin: "users.groupAdmin", member: "users.groupMember", guest: "users.groupGuest" };
function groupLabel(groupName: string) { return groupLabelKeys[groupName] ? t(groupLabelKeys[groupName]) : groupName; }
const roleLabelKeys: Record<string, string> = { admin: "users.roleAdmin", member: "users.roleMember" };
function roleLabel(role: string) { return roleLabelKeys[role] ? t(roleLabelKeys[role]) : role; }
function nodeAccessText(ids?: number[]) {
  if (!ids || ids.length === 0) return t("users.allNodes");
  return ids.map(id => nodes.value.find(node => node.id === id)?.hostname || `#${id}`).join("，");
}

// 待审核用户数（SSO 注册但尚未启用）
const pendingCount = computed(() => users.value.filter(u => !u.enabled).length);
// 表格排序：待审核用户置顶
const sortedUsers = computed(() => [...users.value].sort((a, b) => Number(b.enabled === false) - Number(a.enabled === false)));

async function load(){loading.value=true;try{[users.value,profiles.value,nodes.value]=await Promise.all([getUsers(),getQuotaProfiles(),getNodes()]);}finally{loading.value=false;}}

// 切换分组时自动填充对应预设配额
function applyGroupDefaults(groupName: string) {
  const p = profiles.value.find(p => p.group_name === groupName);
  if (p) { userForm.cpu_cores = p.cpu_cores; userForm.memory_gb = p.memory_gb; userForm.disk_gb = p.disk_gb; userForm.container_disk_limit_gb = p.container_disk_limit_gb; userForm.storage_quota_gb = p.storage_quota_gb; userForm.gpu_count = p.gpu_count; userForm.container_count = p.container_count; userForm.allowed_node_ids = [...(p.allowed_node_ids || [])]; }
}

function openUser(row?:User){
  editingId.value = row?.id ?? null;
  if (row) {
    Object.assign(userForm, row);
    userForm.password = "";
  } else {
    const p = profiles.value.find(p => p.group_name === "member");
    Object.assign(userForm, {username:"",display_name:"",phone:"",email:"",group_name:"member",password:"",ssh_key:"",enabled:true,
      cpu_cores: p?.cpu_cores ?? 16, memory_gb: p?.memory_gb ?? 64, disk_gb: p?.disk_gb ?? 500, container_disk_limit_gb: p?.container_disk_limit_gb ?? 500, storage_quota_gb: p?.storage_quota_gb ?? 500, gpu_count: p?.gpu_count ?? 1, container_count: p?.container_count ?? 2, allowed_node_ids:[...(p?.allowed_node_ids || [])]});
  }
  userVisible.value = true;
}
async function submitUser(){await saveUser(userForm,editingId.value);userVisible.value=false;ElMessage.success(t("users.userSaved"));await load();}
function openProfile(row:QuotaProfile){editingProfile.value=row;Object.assign(profileForm,row);profileVisible.value=true;}
async function submitProfile(){if(!editingProfile.value)return;await saveQuotaProfile(editingProfile.value.group_name,profileForm);profileVisible.value=false;ElMessage.success(t("users.quotaSaved"));await load();}

async function approveUser(row: User) {
  try {
    await ElMessageBox.confirm(t("users.approveConfirm", { name: row.display_name || row.username }), t("users.approveConfirmTitle"), { type: "info" });
  } catch { return; } // 用户取消
  try {
    if (row.pending_sso && !row.id) {
      await approveSsoUser(row.casdoor_id!, row.username, row.display_name, row.email, row.group_name);
    } else {
      await saveUser({ ...row, enabled: true, password: "" }, row.id);
    }
    ElMessage.success(t("users.approveSuccess"));
    await load();
  } catch (e: unknown) {
    const msg = (e as {detail?: string; message?: string})?.detail || (e as {message?: string})?.message || t("users.approveFailed");
    ElMessage.error(msg);
  }
}

async function removeUserAction(row: User) {
  // pending_sso 用户没有本地数据库记录，直接从列表中过滤掉即可
  if (!row.id) {
    try {
      await ElMessageBox.confirm(
        t("users.removePendingConfirm", { name: row.display_name || row.username }),
        t("users.removePendingTitle"),
        { type: "warning", confirmButtonText: t("users.remove"), cancelButtonText: t("common.cancel"), confirmButtonClass: "el-button--danger" }
      );
    } catch { return; }
    users.value = users.value.filter(u => u.casdoor_id !== row.casdoor_id);
    ElMessage.success(t("users.removePendingSuccess"));
    return;
  }
  try {
    await ElMessageBox.confirm(
      t("users.removeUserConfirm", { name: row.display_name || row.username }),
      t("users.removeUserTitle"),
      { type: "warning", confirmButtonText: t("users.remove"), cancelButtonText: t("common.cancel"), confirmButtonClass: "el-button--danger" }
    );
  } catch { return; }
  try {
    await removeUser(row.id);
    ElMessage.success(t("users.removeUserSuccess"));
    await load();
  } catch (e: unknown) {
    const msg = (e as {detail?: string; message?: string})?.detail || (e as {message?: string})?.message || t("users.removeFailed");
    ElMessage.error(msg);
  }
}

onMounted(load);
</script>

<template><div v-loading="loading" class="page-stack">
  <el-card shadow="never"><template #header><div class="card-header"><div>
    <strong>{{ t("users.title") }}</strong>
    <el-badge v-if="pendingCount > 0" :value="pendingCount" type="warning" style="margin-left:8px;margin-right:4px"/>
    <small class="field-hint">{{ pendingCount > 0 ? t("users.pendingPrefix", { count: pendingCount }) : "" }}{{ t("users.intro") }}</small>
  </div><el-button type="primary" :icon="Plus" @click="openUser()">{{ t("users.addUser") }}</el-button></div></template>
    <el-table :data="sortedUsers" stripe row-class-name="(row) => !row.row.enabled ? 'pending-row' : ''">
      <el-table-column prop="username" :label="t('users.username')"/>
      <el-table-column prop="display_name" :label="t('users.displayName')"/>
      <el-table-column prop="phone" :label="t('users.phone')" width="140"/>
      <el-table-column prop="email" :label="t('users.email')" min-width="190"/>
      <el-table-column :label="t('users.group')" width="120"><template #default="{row}">{{ groupLabel(row.group_name) }}</template></el-table-column>
      <el-table-column :label="t('users.quota')" min-width="300"><template #default="{row}">{{ row.cpu_cores }}C / {{ row.memory_gb }}G / {{ t("users.totalDisk") }} {{ row.disk_gb }}G / {{ t("users.dataVolume") }} {{ row.container_disk_limit_gb ? `${row.container_disk_limit_gb}G` : t("users.unlimited") }} / {{ t("users.storage") }} {{ row.storage_quota_gb ? `${row.storage_quota_gb}G` : t("users.unlimited") }} / GPU {{ row.gpu_count }} / {{ t("users.containerCount") }} {{ row.container_count }}</template></el-table-column>
      <el-table-column :label="t('users.availableNodes')" min-width="180"><template #default="{row}">{{ nodeAccessText(row.allowed_node_ids) }}</template></el-table-column>
      <el-table-column :label="t('users.state')" width="100">
        <template #default="{row}">
          <el-tag v-if="row.pending_sso" type="warning">{{ t("users.registered") }}</el-tag>
          <el-tag v-else-if="row.enabled" type="success">{{ t("status.enabled") }}</el-tag>
          <el-tag v-else type="danger">{{ t("users.pendingReview") }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('users.actions')" width="240">
        <template #default="{row}">
          <el-button v-if="!row.enabled" size="small" type="success" :icon="Check" @click="approveUser(row)">{{ t("users.approve") }}</el-button>
          <el-button v-if="row.id" size="small" :icon="Edit" @click="openUser(row)">{{ t("common.edit") }}</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="removeUserAction(row)">{{ t("users.remove") }}</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
  <el-card shadow="never"><template #header><strong>{{ t("users.defaultQuota") }}</strong></template><el-table :data="profiles" stripe><el-table-column :label="t('users.group')"><template #default="{row}">{{ groupLabel(row.group_name) }}</template></el-table-column><el-table-column :label="t('users.role')"><template #default="{row}">{{ roleLabel(row.role) }}</template></el-table-column><el-table-column prop="cpu_cores" label="CPU"/><el-table-column prop="memory_gb" :label="t('users.memoryGb')"/><el-table-column prop="disk_gb" :label="t('users.containerDiskTotalGb')"/><el-table-column prop="container_disk_limit_gb" :label="t('users.singleContainerGb')"/><el-table-column prop="storage_quota_gb" :label="t('users.userDirectoryGb')"/><el-table-column prop="gpu_count" label="GPU"/><el-table-column prop="container_count" :label="t('users.containerCount')"/><el-table-column :label="t('users.availableNodes')" min-width="180"><template #default="{row}">{{ nodeAccessText(row.allowed_node_ids) }}</template></el-table-column><el-table-column :label="t('users.actions')"><template #default="{row}"><el-button size="small" :icon="Setting" @click="openProfile(row)">{{ t("users.configure") }}</el-button></template></el-table-column></el-table></el-card>
  <el-dialog v-model="userVisible" :title="editingId?t('users.editUser'):t('users.addUser')" width="760px"><el-form :model="userForm" label-position="top" class="form-grid"><el-form-item :label="t('users.username')"><el-input v-model="userForm.username"/></el-form-item><el-form-item :label="t('users.displayName')"><el-input v-model="userForm.display_name"/></el-form-item><el-form-item :label="t('users.phone')"><el-input v-model="userForm.phone"/></el-form-item><el-form-item :label="t('users.email')"><el-input v-model="userForm.email"/></el-form-item><el-form-item :label="t('users.group')"><el-select v-model="userForm.group_name" @change="applyGroupDefaults"><el-option :label="t('users.groupPlatformAdmin')" value="platform_admin"/><el-option :label="t('users.groupAdmin')" value="admin"/><el-option :label="t('users.groupMember')" value="member"/><el-option :label="t('users.groupGuest')" value="guest"/></el-select></el-form-item><el-form-item :label="editingId?t('users.resetPassword'):t('users.initialPassword')"><el-input v-model="userForm.password" type="password" show-password/></el-form-item><el-form-item label="CPU"><el-input-number v-model="userForm.cpu_cores" :min="0"/></el-form-item><el-form-item :label="t('users.memoryGb')"><el-input-number v-model="userForm.memory_gb" :min="0"/></el-form-item><el-form-item :label="t('users.containerDiskQuotaGb')"><el-input-number v-model="userForm.disk_gb" :min="0"/></el-form-item><el-form-item :label="t('users.containerDataVolumeLimitGb')"><el-input-number v-model="userForm.container_disk_limit_gb" :min="0"/></el-form-item><el-form-item :label="t('users.storageUserDirLimitGb')"><el-input-number v-model="userForm.storage_quota_gb" :min="0"/></el-form-item><el-form-item label="GPU"><el-input-number v-model="userForm.gpu_count" :min="0"/></el-form-item><el-form-item :label="t('users.containerCount')"><el-input-number v-model="userForm.container_count" :min="0"/></el-form-item><el-form-item :label="t('users.availableNodes')" class="wide"><el-select v-model="userForm.allowed_node_ids" multiple collapse-tags collapse-tags-tooltip><el-option v-for="node in nodes" :key="node.id" :label="node.hostname" :value="node.id"/></el-select><small class="field-hint">{{ t("users.noNodeOverrideHint") }}</small></el-form-item><el-form-item :label="t('users.enabled')"><el-switch v-model="userForm.enabled"/></el-form-item><el-form-item :label="t('users.sshPublicKey')" class="wide"><el-input v-model="userForm.ssh_key" type="textarea"/></el-form-item></el-form><template #footer><el-button :icon="Close" @click="userVisible=false">{{ t("common.cancel") }}</el-button><el-button type="primary" :icon="Select" @click="submitUser">{{ t("common.save") }}</el-button></template></el-dialog>
  <el-dialog v-model="profileVisible" :title="t('users.defaultQuotaFor', { group: groupLabel(editingProfile?.group_name||'') })" width="600px"><el-form :model="profileForm" label-position="top" class="form-grid"><el-form-item :label="t('users.role')"><el-select v-model="profileForm.role"><el-option :label="t('users.roleAdmin')" value="admin"/><el-option :label="t('users.roleUser')" value="member"/></el-select></el-form-item><el-form-item label="CPU"><el-input-number v-model="profileForm.cpu_cores" :min="0"/></el-form-item><el-form-item :label="t('users.memoryGb')"><el-input-number v-model="profileForm.memory_gb" :min="0"/></el-form-item><el-form-item :label="t('users.containerDiskQuotaGb')"><el-input-number v-model="profileForm.disk_gb" :min="0"/></el-form-item><el-form-item :label="t('users.containerDataVolumeLimitGb')"><el-input-number v-model="profileForm.container_disk_limit_gb" :min="0"/></el-form-item><el-form-item :label="t('users.storageUserDirLimitGb')"><el-input-number v-model="profileForm.storage_quota_gb" :min="0"/></el-form-item><el-form-item label="GPU"><el-input-number v-model="profileForm.gpu_count" :min="0"/></el-form-item><el-form-item :label="t('users.containerCount')"><el-input-number v-model="profileForm.container_count" :min="0"/></el-form-item><el-form-item :label="t('users.availableNodes')" class="wide"><el-select v-model="profileForm.allowed_node_ids" multiple collapse-tags collapse-tags-tooltip><el-option v-for="node in nodes" :key="node.id" :label="node.hostname" :value="node.id"/></el-select><small class="field-hint">{{ t("users.noGroupNodeHint") }}</small></el-form-item></el-form><template #footer><el-button :icon="Close" @click="profileVisible=false">{{ t("common.cancel") }}</el-button><el-button type="primary" :icon="Select" @click="submitProfile">{{ t("common.save") }}</el-button></template></el-dialog>
</div></template>
