<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { Select } from "@element-plus/icons-vue";
import { getPlatformSettings, updatePlatformSettings, type PlatformSettings } from "../api/cluster";

const loading = ref(false);
const saving = ref(false);
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
  sso_provider_enabled: false,
  sso_provider_type: "oidc",
  sso_provider_name: "casdoor",
  sso_provider_display_name: "统一认证",
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

const groupOptions = [
  { label: "平台管理员", value: "platform_admin" },
  { label: "管理员", value: "admin" },
  { label: "成员", value: "member" },
  { label: "来宾", value: "guest" },
] as const;

async function load() {
  loading.value = true;
  try {
    Object.assign(form, await getPlatformSettings());
  } finally {
    loading.value = false;
  }
}

async function submit() {
  saving.value = true;
  try {
    Object.assign(form, await updatePlatformSettings({ ...form }));
    ElMessage.success("平台设置已保存");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <el-card shadow="never">
      <template #header><strong>认证与注册</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item label="平台账号登录">
          <el-switch v-model="form.local_login_enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="允许平台注册">
          <el-switch v-model="form.platform_registration_enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="平台注册自动启用">
          <el-switch v-model="form.platform_registration_auto_enable" active-text="自动启用" inactive-text="管理员审核" />
        </el-form-item>
        <el-form-item label="平台注册默认分组">
          <el-select v-model="form.platform_registration_default_group">
            <el-option v-for="item in groupOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>SSO 用户策略</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item label="允许 SSO 注册入口">
          <el-switch v-model="form.sso_registration_enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="SSO 首次登录自动创建用户">
          <el-switch v-model="form.sso_auto_create_users" active-text="自动创建" inactive-text="仅允许已有账号" />
        </el-form-item>
        <el-form-item label="SSO 新用户自动启用">
          <el-switch v-model="form.sso_auto_enable_new_users" active-text="自动启用" inactive-text="管理员审核" />
        </el-form-item>
        <el-form-item label="SSO 默认分组">
          <el-select v-model="form.sso_default_group">
            <el-option v-for="item in groupOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>平台策略</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item label="平台时区">
          <el-input v-model="form.platform_timezone" placeholder="Asia/Shanghai" />
        </el-form-item>
        <el-form-item label="传输带宽上限 Mbps">
          <el-input-number v-model="form.transfer_bandwidth_limit_mbps" :min="0" :max="100000" :step="10" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header><strong>SSO Provider 配置</strong></template>
      <el-form :model="form" label-position="top" class="settings-grid">
        <el-form-item label="启用 SSO 登录入口">
          <el-switch v-model="form.sso_provider_enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="Provider 类型">
          <el-segmented v-model="form.sso_provider_type" :options="[{ label: 'OIDC', value: 'oidc' }, { label: 'CAS', value: 'cas' }]" />
        </el-form-item>
        <el-form-item label="Provider 标识">
          <el-input v-model="form.sso_provider_name" placeholder="casdoor" />
        </el-form-item>
        <el-form-item label="登录按钮名称">
          <el-input v-model="form.sso_provider_display_name" placeholder="统一认证" />
        </el-form-item>
        <el-form-item label="回调基础地址">
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
        <el-form-item label="授权端点">
          <el-input v-model="form.sso_oidc_authorization_endpoint" placeholder="可选，Discovery 不可用时填写" />
        </el-form-item>
        <el-form-item label="令牌端点">
          <el-input v-model="form.sso_oidc_token_endpoint" placeholder="可选，Discovery 不可用时填写" />
        </el-form-item>
        <el-form-item label="用户信息端点">
          <el-input v-model="form.sso_oidc_userinfo_endpoint" placeholder="可选，Discovery 不可用时填写" />
        </el-form-item>
      </el-form>

      <el-form v-else :model="form" label-position="top" class="settings-grid">
        <el-form-item label="CAS 服务地址">
          <el-input v-model="form.sso_cas_server_url" placeholder="https://auth.example.com/cas" />
        </el-form-item>
        <el-form-item label="CAS 版本">
          <el-input-number v-model="form.sso_cas_version" :min="2" :max="3" />
        </el-form-item>
      </el-form>
    </el-card>

    <div class="settings-actions">
      <el-button type="primary" :icon="Select" :loading="saving" @click="submit">保存设置</el-button>
    </div>
  </div>
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
