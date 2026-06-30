<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Check, Close, Delete, Edit, Plus, Select, Setting } from "@element-plus/icons-vue";
import { getNodes, getQuotaProfiles, getUsers, saveQuotaProfile, saveUser, approveSsoUser, removeUser, type Node, type QuotaProfile, type User } from "../api/cluster";

const loading = ref(false); const users = ref<User[]>([]); const profiles = ref<QuotaProfile[]>([]); const nodes = ref<Node[]>([]);
const userVisible = ref(false); const profileVisible = ref(false); const editingId = ref<number | null>(); const editingProfile = ref<QuotaProfile>();
const userForm = reactive({ username:"",display_name:"",phone:"",email:"",group_name:"member",password:"",ssh_key:"",enabled:true,cpu_cores:16,memory_gb:64,disk_gb:500,container_disk_limit_gb:500,storage_quota_gb:500,gpu_count:1,container_count:2,allowed_node_ids:[] as number[] });
const profileForm = reactive({ role:"member" as "admin"|"member",cpu_cores:16,memory_gb:64,disk_gb:500,container_disk_limit_gb:500,storage_quota_gb:500,gpu_count:1,container_count:2,allowed_node_ids:[] as number[] });

const groupLabels: Record<string, string> = { platform_admin: "平台管理员", admin: "管理员", member: "成员", guest: "来宾" };
function groupLabel(groupName: string) { return groupLabels[groupName] || groupName; }
const roleLabels: Record<string, string> = { admin: "管理员", member: "成员" };
function roleLabel(role: string) { return roleLabels[role] || role; }
function nodeAccessText(ids?: number[]) {
  if (!ids || ids.length === 0) return "全部节点";
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
async function submitUser(){await saveUser(userForm,editingId.value);userVisible.value=false;ElMessage.success("用户已保存");await load();}
function openProfile(row:QuotaProfile){editingProfile.value=row;Object.assign(profileForm,row);profileVisible.value=true;}
async function submitProfile(){if(!editingProfile.value)return;await saveQuotaProfile(editingProfile.value.group_name,profileForm);profileVisible.value=false;ElMessage.success("默认配额已保存");await load();}

async function approveUser(row: User) {
  try {
    await ElMessageBox.confirm(`确认审核通过并启用用户「${row.display_name || row.username}」？`, "审核确认", { type: "info" });
  } catch { return; } // 用户取消
  try {
    if (row.pending_sso && !row.id) {
      await approveSsoUser(row.casdoor_id!, row.username, row.display_name, row.email, row.group_name);
    } else {
      await saveUser({ ...row, enabled: true, password: "" }, row.id);
    }
    ElMessage.success("已审核通过，用户现在可以登录");
    await load();
  } catch (e: unknown) {
    const msg = (e as {detail?: string; message?: string})?.detail || (e as {message?: string})?.message || "审核失败，请重试";
    ElMessage.error(msg);
  }
}

async function removeUserAction(row: User) {
  // pending_sso 用户没有本地数据库记录，直接从列表中过滤掉即可
  if (!row.id) {
    try {
      await ElMessageBox.confirm(
        `确认拒绝并移除待审核用户「${row.display_name || row.username}」？\n该用户将无法再出现在审核列表中。`,
        "移除待审核用户",
        { type: "warning", confirmButtonText: "移除", cancelButtonText: "取消", confirmButtonClass: "el-button--danger" }
      );
    } catch { return; }
    users.value = users.value.filter(u => u.casdoor_id !== row.casdoor_id);
    ElMessage.success("待审核用户已移除");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确认移除用户「${row.display_name || row.username}」？\n其容器、镜像和共享资源将转移给管理员，用户账号将被禁用。`,
      "移除用户",
      { type: "warning", confirmButtonText: "移除", cancelButtonText: "取消", confirmButtonClass: "el-button--danger" }
    );
  } catch { return; }
  try {
    await removeUser(row.id);
    ElMessage.success("用户已移除，资源已转移给管理员");
    await load();
  } catch (e: unknown) {
    const msg = (e as {detail?: string; message?: string})?.detail || (e as {message?: string})?.message || "移除失败，请重试";
    ElMessage.error(msg);
  }
}

onMounted(load);
</script>

<template><div v-loading="loading" class="page-stack">
  <el-card shadow="never"><template #header><div class="card-header"><div>
    <strong>用户管理</strong>
    <el-badge v-if="pendingCount > 0" :value="pendingCount" type="warning" style="margin-left:8px;margin-right:4px"/>
    <small class="field-hint">{{ pendingCount > 0 ? `${pendingCount} 人待审核 —` : '' }} 普通用户可自助注册；管理员可调整档案、分组和个人额度。</small>
  </div><el-button type="primary" :icon="Plus" @click="openUser()">添加用户</el-button></div></template>
    <el-table :data="sortedUsers" stripe row-class-name="(row) => !row.row.enabled ? 'pending-row' : ''">
      <el-table-column prop="username" label="用户名"/>
      <el-table-column prop="display_name" label="用户名称"/>
      <el-table-column prop="phone" label="联系电话" width="140"/>
      <el-table-column prop="email" label="联系邮箱" min-width="190"/>
      <el-table-column label="分组" width="120"><template #default="{row}">{{ groupLabel(row.group_name) }}</template></el-table-column>
      <el-table-column label="额度" min-width="300"><template #default="{row}">{{ row.cpu_cores }}C / {{ row.memory_gb }}G / 总磁盘 {{ row.disk_gb }}G / 数据卷 {{ row.container_disk_limit_gb ? `${row.container_disk_limit_gb}G` : '不限' }} / 存储 {{ row.storage_quota_gb ? `${row.storage_quota_gb}G` : '不限' }} / GPU {{ row.gpu_count }} / 容器 {{ row.container_count }}</template></el-table-column>
      <el-table-column label="可用节点" min-width="180"><template #default="{row}">{{ nodeAccessText(row.allowed_node_ids) }}</template></el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{row}">
          <el-tag v-if="row.pending_sso" type="warning">已注册</el-tag>
          <el-tag v-else-if="row.enabled" type="success">已启用</el-tag>
          <el-tag v-else type="danger">待审核</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240">
        <template #default="{row}">
          <el-button v-if="!row.enabled" size="small" type="success" :icon="Check" @click="approveUser(row)">审核通过</el-button>
          <el-button v-if="row.id" size="small" :icon="Edit" @click="openUser(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="removeUserAction(row)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
  <el-card shadow="never"><template #header><strong>分组默认配额</strong></template><el-table :data="profiles" stripe><el-table-column label="分组"><template #default="{row}">{{ groupLabel(row.group_name) }}</template></el-table-column><el-table-column label="角色"><template #default="{row}">{{ roleLabel(row.role) }}</template></el-table-column><el-table-column prop="cpu_cores" label="CPU"/><el-table-column prop="memory_gb" label="内存 GB"/><el-table-column prop="disk_gb" label="容器磁盘总额 GB"/><el-table-column prop="container_disk_limit_gb" label="单容器 GB"/><el-table-column prop="storage_quota_gb" label="用户目录 GB"/><el-table-column prop="gpu_count" label="GPU"/><el-table-column prop="container_count" label="容器数"/><el-table-column label="可用节点" min-width="180"><template #default="{row}">{{ nodeAccessText(row.allowed_node_ids) }}</template></el-table-column><el-table-column label="操作"><template #default="{row}"><el-button size="small" :icon="Setting" @click="openProfile(row)">配置</el-button></template></el-table-column></el-table></el-card>
  <el-dialog v-model="userVisible" :title="editingId?'编辑用户':'添加用户'" width="760px"><el-form :model="userForm" label-position="top" class="form-grid"><el-form-item label="用户名"><el-input v-model="userForm.username"/></el-form-item><el-form-item label="用户名称"><el-input v-model="userForm.display_name"/></el-form-item><el-form-item label="联系电话"><el-input v-model="userForm.phone"/></el-form-item><el-form-item label="联系邮箱"><el-input v-model="userForm.email"/></el-form-item><el-form-item label="分组"><el-select v-model="userForm.group_name" @change="applyGroupDefaults"><el-option label="平台管理员" value="platform_admin"/><el-option label="管理员" value="admin"/><el-option label="成员" value="member"/><el-option label="来宾" value="guest"/></el-select></el-form-item><el-form-item :label="editingId?'重置密码（留空不修改）':'初始密码'"><el-input v-model="userForm.password" type="password" show-password/></el-form-item><el-form-item label="CPU"><el-input-number v-model="userForm.cpu_cores" :min="0"/></el-form-item><el-form-item label="内存 GB"><el-input-number v-model="userForm.memory_gb" :min="0"/></el-form-item><el-form-item label="容器磁盘总额度 GB"><el-input-number v-model="userForm.disk_gb" :min="0"/></el-form-item><el-form-item label="单容器数据卷容量上限 GB"><el-input-number v-model="userForm.container_disk_limit_gb" :min="0"/></el-form-item><el-form-item label="存储节点用户目录上限 GB"><el-input-number v-model="userForm.storage_quota_gb" :min="0"/></el-form-item><el-form-item label="GPU"><el-input-number v-model="userForm.gpu_count" :min="0"/></el-form-item><el-form-item label="容器数"><el-input-number v-model="userForm.container_count" :min="0"/></el-form-item><el-form-item label="可用节点" class="wide"><el-select v-model="userForm.allowed_node_ids" multiple collapse-tags collapse-tags-tooltip><el-option v-for="node in nodes" :key="node.id" :label="node.hostname" :value="node.id"/></el-select><small class="field-hint">不选择表示使用全部节点；个人配置会覆盖分组默认节点</small></el-form-item><el-form-item label="启用"><el-switch v-model="userForm.enabled"/></el-form-item><el-form-item label="SSH 公钥" class="wide"><el-input v-model="userForm.ssh_key" type="textarea"/></el-form-item></el-form><template #footer><el-button :icon="Close" @click="userVisible=false">取消</el-button><el-button type="primary" :icon="Select" @click="submitUser">保存</el-button></template></el-dialog>
  <el-dialog v-model="profileVisible" :title="`默认配额 · ${groupLabel(editingProfile?.group_name||'')}`" width="600px"><el-form :model="profileForm" label-position="top" class="form-grid"><el-form-item label="角色"><el-select v-model="profileForm.role"><el-option label="管理员" value="admin"/><el-option label="普通用户" value="member"/></el-select></el-form-item><el-form-item label="CPU"><el-input-number v-model="profileForm.cpu_cores" :min="0"/></el-form-item><el-form-item label="内存 GB"><el-input-number v-model="profileForm.memory_gb" :min="0"/></el-form-item><el-form-item label="容器磁盘总额度 GB"><el-input-number v-model="profileForm.disk_gb" :min="0"/></el-form-item><el-form-item label="单容器数据卷容量上限 GB"><el-input-number v-model="profileForm.container_disk_limit_gb" :min="0"/></el-form-item><el-form-item label="存储节点用户目录上限 GB"><el-input-number v-model="profileForm.storage_quota_gb" :min="0"/></el-form-item><el-form-item label="GPU"><el-input-number v-model="profileForm.gpu_count" :min="0"/></el-form-item><el-form-item label="容器数"><el-input-number v-model="profileForm.container_count" :min="0"/></el-form-item><el-form-item label="可用节点" class="wide"><el-select v-model="profileForm.allowed_node_ids" multiple collapse-tags collapse-tags-tooltip><el-option v-for="node in nodes" :key="node.id" :label="node.hostname" :value="node.id"/></el-select><small class="field-hint">不选择表示该分组默认可使用全部节点</small></el-form-item></el-form><template #footer><el-button :icon="Close" @click="profileVisible=false">取消</el-button><el-button type="primary" :icon="Select" @click="submitProfile">保存</el-button></template></el-dialog>
</div></template>
