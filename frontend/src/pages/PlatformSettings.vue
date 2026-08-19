<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Select } from "@element-plus/icons-vue";
import TasksPage from "./Tasks.vue";
import { getPlatformSettings, updatePlatformSettings, type PlatformSettings } from "../api/cluster";

const { t } = useI18n();
const loading = ref(false);
const saving = ref(false);
const canaryUserIdsText = ref("");
const form = reactive<PlatformSettings>({
  local_login_enabled: true,
  platform_registration_enabled: false,
  platform_registration_auto_enable: false,
  platform_registration_default_group: "member",
  sso_registration_enabled: true,
  sso_auto_create_users: true,
  sso_auto_enable_new_users: false,
  sso_default_group: "member",
  platform_timezone: "Asia/Shanghai",
  transfer_bandwidth_limit_mbps: 0,
  shared_storage_mode: "disabled",
  shared_storage_canary_user_ids: [],
  nfs_server: "",
  nfs_users_export: "",
  nfs_datasets_export: "",
  nfs_models_export: "",
  nfs_mount_options: "hard,_netdev,noatime,vers=4.1,proto=tcp",
  nfs_sentinel: ".server-vps-nfs",
  nfs_sentinel_signature: "",
  nfs_idmap_base: 1000000,
  truenas_nfs_auto_share: false,
  workspace_default_gb: 100,
  workspace_retention_days: 30,
  agent_metrics_interval_seconds: 2,
  agent_heartbeat_interval_seconds: 15,
  agent_container_interval_seconds: 15,
  agent_storage_interval_seconds: 60,
  agent_inventory_interval_seconds: 300,
  agent_task_poll_interval_seconds: 5,
  webhook_enabled: false,
  webhook_url: "",
  webhook_secret: "",
  sso_provider_enabled: false,
  sso_provider_type: "oidc",
  sso_provider_name: "casdoor",
  sso_provider_display_name: t("settings.defaultLoginButtonName"),
  sso_callback_base_url: "",
  sso_cas_server_url: "",
  sso_cas_version: 3,
  sso_oidc_issuer: "",
  sso_oidc_authorization_endpoint: "",
  sso_oidc_token_endpoint: "",
  sso_oidc_userinfo_endpoint: "",
  sso_oidc_client_id: "",
  sso_oidc_client_secret: "",
  sso_oidc_scopes: "openid profile email",
  sso_casdoor_admin_owner: "built-in",
});

const groupOptions = ["platform_admin", "admin", "member", "guest"] as const;
const groupLabelKeys: Record<string, string> = {
  platform_admin: "users.groupPlatformAdmin",
  admin: "users.groupAdmin",
  member: "users.groupMember",
  guest: "users.groupGuest",
};

async function load() {
  loading.value = true;
  try {
    Object.assign(form, await getPlatformSettings());
    canaryUserIdsText.value = form.shared_storage_canary_user_ids.join(",");
  } finally {
    loading.value = false;
  }
}

async function submit() {
  saving.value = true;
  try {
    form.shared_storage_canary_user_ids = Array.from(new Set(
      canaryUserIdsText.value
        .split(/[,，\s]+/)
        .map((value) => Number.parseInt(value.trim(), 10))
        .filter((value) => Number.isInteger(value) && value > 0),
    ));
    Object.assign(form, await updatePlatformSettings({ ...form }));
    canaryUserIdsText.value = form.shared_storage_canary_user_ids.join(",");
    ElMessage.success(t("settings.saved"));
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t("settings.saveFailed"));
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <el-tabs>
    <el-tab-pane :label="t('settings.tabSettings')">
      <div v-loading="loading" class="page-stack" style="margin-top:16px">
    <el-card shadow="never">
      <template #header><strong>{{ t("settings.authRegistration") }}</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.platformLogin')">
          <el-switch v-model="form.local_login_enabled" :active-text="t('settings.enable')" :inactive-text="t('settings.disable')" />
        </el-form-item>
        <el-form-item :label="t('settings.allowPlatformRegistration')">
          <el-switch v-model="form.platform_registration_enabled" :active-text="t('settings.enable')" :inactive-text="t('settings.disable')" />
        </el-form-item>
        <el-form-item :label="t('settings.platformRegistrationAutoEnable')">
          <el-switch v-model="form.platform_registration_auto_enable" :active-text="t('settings.autoEnable')" :inactive-text="t('settings.adminReview')" />
        </el-form-item>
        <el-form-item :label="t('settings.platformRegistrationDefaultGroup')">
          <el-select v-model="form.platform_registration_default_group">
            <el-option v-for="item in groupOptions" :key="item" :label="t(groupLabelKeys[item])" :value="item" />
          </el-select>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>{{ t("settings.ssoUserPolicy") }}</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.allowSsoRegistration')">
          <el-switch v-model="form.sso_registration_enabled" :active-text="t('settings.enable')" :inactive-text="t('settings.disable')" />
        </el-form-item>
        <el-form-item :label="t('settings.ssoAutoCreateUsers')">
          <el-switch v-model="form.sso_auto_create_users" :active-text="t('settings.autoCreate')" :inactive-text="t('settings.existingOnly')" />
        </el-form-item>
        <el-form-item :label="t('settings.ssoAutoEnableNewUsers')">
          <el-switch v-model="form.sso_auto_enable_new_users" :active-text="t('settings.autoEnable')" :inactive-text="t('settings.adminReview')" />
        </el-form-item>
        <el-form-item :label="t('settings.ssoDefaultGroup')">
          <el-select v-model="form.sso_default_group">
            <el-option v-for="item in groupOptions" :key="item" :label="t(groupLabelKeys[item])" :value="item" />
          </el-select>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>{{ t("settings.ssoProviderConfig") }}</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.enableSsoLogin')">
          <el-switch v-model="form.sso_provider_enabled" :active-text="t('settings.enable')" :inactive-text="t('settings.disable')" />
        </el-form-item>
        <el-form-item :label="t('settings.providerType')">
          <el-segmented v-model="form.sso_provider_type" :options="[{ label: 'OIDC', value: 'oidc' }, { label: 'CAS', value: 'cas' }]" />
        </el-form-item>
        <el-form-item :label="t('settings.providerName')">
          <el-input v-model="form.sso_provider_name" placeholder="casdoor" />
        </el-form-item>
        <el-form-item :label="t('settings.loginButtonName')">
          <el-input v-model="form.sso_provider_display_name" :placeholder="t('settings.defaultLoginButtonName')" />
        </el-form-item>
        <el-form-item :label="t('settings.callbackBaseUrl')">
          <el-input v-model="form.sso_callback_base_url" placeholder="https://cluster.example.com" />
        </el-form-item>
        <el-form-item label="Casdoor Owner">
          <el-input v-model="form.sso_casdoor_admin_owner" placeholder="built-in" />
        </el-form-item>
      </el-form>

      <el-form v-if="form.sso_provider_type === 'oidc'" :model="form" label-position="top" class="settings-grid">
        <el-form-item label="OIDC Issuer">
          <el-input v-model="form.sso_oidc_issuer" placeholder="https://auth.example.com" />
        </el-form-item>
        <el-form-item label="Client ID">
          <el-input v-model="form.sso_oidc_client_id" />
        </el-form-item>
        <el-form-item label="Client Secret">
          <el-input v-model="form.sso_oidc_client_secret" type="password" show-password />
        </el-form-item>
        <el-form-item label="Scopes">
          <el-input v-model="form.sso_oidc_scopes" placeholder="openid profile email" />
        </el-form-item>
        <el-form-item :label="t('settings.authorizationEndpoint')">
          <el-input v-model="form.sso_oidc_authorization_endpoint" :placeholder="t('settings.optionalDiscoveryEndpoint')" />
        </el-form-item>
        <el-form-item :label="t('settings.tokenEndpoint')">
          <el-input v-model="form.sso_oidc_token_endpoint" :placeholder="t('settings.optionalDiscoveryEndpoint')" />
        </el-form-item>
        <el-form-item :label="t('settings.userinfoEndpoint')">
          <el-input v-model="form.sso_oidc_userinfo_endpoint" :placeholder="t('settings.optionalDiscoveryEndpoint')" />
        </el-form-item>
      </el-form>

      <el-form v-else :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.casServerUrl')">
          <el-input v-model="form.sso_cas_server_url" placeholder="https://auth.example.com/cas" />
        </el-form-item>
        <el-form-item :label="t('settings.casVersion')">
          <el-input-number v-model="form.sso_cas_version" :min="2" :max="3" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>{{ t("settings.platformPolicy") }}</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.platformTimezone')">
          <el-input v-model="form.platform_timezone" placeholder="Asia/Shanghai" />
        </el-form-item>
        <el-form-item :label="t('settings.transferBandwidthLimit')">
          <el-input-number v-model="form.transfer_bandwidth_limit_mbps" :min="0" :max="100000" :step="10" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>共享 NFS</strong></template>
      <el-alert title="先升级全部计算节点 agent 并完成 NFS/sentinel 验证，再从 canary 切换到 enabled。" type="warning" :closable="false" style="margin-bottom:16px" />
      <el-alert
        :title="`当前开关：shared_storage_mode=${form.shared_storage_mode}`"
        :type="form.shared_storage_mode === 'disabled' ? 'success' : 'warning'"
        :closable="false"
        show-icon
        style="margin-bottom:16px"
      />
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item label="共享存储模式（shared_storage_mode）">
          <el-select v-model="form.shared_storage_mode">
            <el-option label="关闭" value="disabled" /><el-option label="Canary" value="canary" /><el-option label="全部启用" value="enabled" />
          </el-select>
        </el-form-item>
        <el-form-item label="Canary 用户 ID（逗号分隔）">
          <el-input v-model="canaryUserIdsText" placeholder="例如：1, 7, 12" />
          <div class="form-help">支持英文逗号、中文逗号或空格；留空表示不允许任何继承全局 Canary 策略的用户。</div>
        </el-form-item>
        <el-form-item label="NFS 服务地址"><el-input v-model="form.nfs_server" placeholder="10.20.0.10" /></el-form-item>
		<el-form-item label="用户父 Share 模板"><el-input v-model="form.nfs_users_export" placeholder="/mnt/pool/cluster-storage/users（用于克隆访问白名单）" /></el-form-item>
		<el-form-item label="自动创建 TrueNAS 用户 NFS Share"><el-switch v-model="form.truenas_nfs_auto_share" /><div class="form-help">以“用户父导出”对应的现有 Share 为访问白名单模板；模板未限制 hosts/networks 或启用了 root 映射时会安全失败。</div></el-form-item>
        <el-form-item label="数据集导出"><el-input v-model="form.nfs_datasets_export" placeholder="/mnt/pool/cluster-storage/datasets" /></el-form-item>
        <el-form-item label="模型导出"><el-input v-model="form.nfs_models_export" placeholder="/mnt/pool/cluster-storage/models" /></el-form-item>
        <el-form-item label="NFS 挂载参数"><el-input v-model="form.nfs_mount_options" /></el-form-item>
        <el-form-item label="Sentinel 文件"><el-input v-model="form.nfs_sentinel" /></el-form-item>
        <el-form-item label="Sentinel 签名"><el-input v-model="form.nfs_sentinel_signature" type="password" show-password placeholder="与三个 NFS 导出根目录文件内容一致" /></el-form-item>
        <el-form-item label="Incus idmap base"><el-input-number v-model="form.nfs_idmap_base" :min="65536" :max="2000000000" /></el-form-item>
        <el-form-item label="Workspace 默认上限 GiB"><el-input-number v-model="form.workspace_default_gb" :min="1" /></el-form-item>
        <el-form-item label="临时 Workspace 保留天数"><el-input-number v-model="form.workspace_retention_days" :min="1" /></el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>{{ t("settings.agentCollectionPolicy") }}</strong></template>
      <el-alert :title="t('settings.agentCollectionHint')" type="info" :closable="false" show-icon style="margin-bottom:16px" />
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item :label="t('settings.metricsInterval')">
          <el-input-number v-model="form.agent_metrics_interval_seconds" :min="1" :max="60" />
        </el-form-item>
        <el-form-item :label="t('settings.heartbeatInterval')">
          <el-input-number v-model="form.agent_heartbeat_interval_seconds" :min="5" :max="300" />
        </el-form-item>
        <el-form-item :label="t('settings.containerInterval')">
          <el-input-number v-model="form.agent_container_interval_seconds" :min="form.agent_heartbeat_interval_seconds" :max="300" />
        </el-form-item>
        <el-form-item :label="t('settings.storageInterval')">
          <el-input-number v-model="form.agent_storage_interval_seconds" :min="30" :max="3600" :step="10" />
        </el-form-item>
        <el-form-item :label="t('settings.inventoryInterval')">
          <el-input-number v-model="form.agent_inventory_interval_seconds" :min="60" :max="86400" :step="60" />
        </el-form-item>
        <el-form-item :label="t('settings.taskPollInterval')">
          <el-input-number v-model="form.agent_task_poll_interval_seconds" :min="1" :max="60" />
        </el-form-item>
      </el-form>
    </el-card>

    <!-- Webhook 通知 -->
    <el-card shadow="never">
      <template #header><strong>Webhook 通知</strong></template>
      <el-form :model="form" label-width="130px">
        <el-form-item label="启用 Webhook">
          <el-switch v-model="form.webhook_enabled" />
          <span style="margin-left:8px;color:var(--el-text-color-secondary);font-size:13px">开启后，当集群告警触发时自动向 Webhook URL 发送 POST 请求</span>
        </el-form-item>
        <el-form-item label="Webhook URL">
          <el-input v-model="form.webhook_url" placeholder="https://your-webhook-endpoint/notify" :disabled="!form.webhook_enabled" />
        </el-form-item>
        <el-form-item label="签名密钥（可选）">
          <el-input v-model="form.webhook_secret" placeholder="留空则不添加签名头" type="password" show-password :disabled="!form.webhook_enabled" />
          <div style="margin-top:4px;color:var(--el-text-color-secondary);font-size:12px">设置后，每次请求会在 <code>X-Webhook-Secret</code> 头携带该密钥供接收方验签</div>
        </el-form-item>
      </el-form>
    </el-card>

    <div class="settings-actions">
        <el-button type="primary" :icon="Select" :loading="saving" @click="submit">{{ t("settings.saveSettings") }}</el-button>
      </div>
    </div>
    </el-tab-pane>
    <el-tab-pane :label="t('nav.tasks')">
      <TasksPage />
    </el-tab-pane>
  </el-tabs>
</template>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 4px 20px;
}
.settings-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
