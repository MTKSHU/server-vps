<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Plus, Promotion } from "@element-plus/icons-vue";
import { login, getAuthConfig, getSSOProviders, ssoStartUrl, type AuthConfig, type SSOProvider } from "../api/cluster";
import { setAuth } from "../auth";

const router = useRouter();
const route = useRoute();
const loading = ref(false);
const form = reactive({ username: "", password: "" });
const ssoProviders = ref<SSOProvider[]>([]);
const authConfig = ref<AuthConfig | null>(null);
const isPlatformLogin = computed(() => route.name === "platformLogin");
const activeTab = ref(isPlatformLogin.value ? "platform" : "sso");
const showPlatformRegister = computed(() =>
  authConfig.value?.registration_enabled &&
  authConfig.value.registration_mode === "platform"
);

onMounted(async () => {
  try { authConfig.value = await getAuthConfig(); } catch { /* ignore */ }
  try { ssoProviders.value = await getSSOProviders(); } catch { /* ignore */ }
});

function loginWithSSO(providerId: string) {
  window.location.href = ssoStartUrl(providerId);
}

async function submitLocal() {
  if (authConfig.value && !authConfig.value.local_login_enabled) {
    ElMessage.error("平台账号登录未启用");
    return;
  }
  loading.value = true;
  try {
    const result = await login(form.username, form.password);
    setAuth(result.token, result.user);
    await router.replace((route.query.redirect as string) || { name: "dashboard" });
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : "登录失败");
  } finally { loading.value = false; }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <h1>GPU 集群平台</h1>
      <p>请选择登录方式</p>

      <el-tabs v-model="activeTab" stretch class="login-tabs">
        <el-tab-pane label="机构登录" name="sso">
          <div class="sso-section primary-sso">
            <el-button
              v-for="p in ssoProviders"
              :key="p.id"
              class="sso-btn"
              type="primary"
              size="large"
              :icon="Promotion"
              data-keep-label="true"
              @click="loginWithSSO(p.id)"
            >
              {{ p.display_name || "机构统一认证" }}
            </el-button>
            <el-empty v-if="ssoProviders.length === 0" description="暂无可用统一认证入口" />
          </div>
        </el-tab-pane>

        <el-tab-pane label="平台登录" name="platform">
          <el-form :model="form" label-position="top" class="login-form" @submit.prevent="submitLocal">
            <template v-if="!authConfig || authConfig.local_login_enabled">
              <el-form-item label="用户名"><el-input v-model="form.username" autocomplete="username" /></el-form-item>
              <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password autocomplete="current-password" @keyup.enter="submitLocal" /></el-form-item>
              <el-button type="primary" class="login-submit" :loading="loading" @click="submitLocal">
                登录
              </el-button>
            </template>
            <el-empty v-else description="平台账号登录未启用" />
            <el-button
              v-if="showPlatformRegister"
              class="register-link"
              :icon="Plus"
              data-keep-label="true"
              @click="router.push({ name: 'register' })"
            >
              注册平台账号
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.login-form {
  margin-top: 20px;
}
.login-tabs {
  margin-top: 18px;
}
.login-submit {
  width: 100%;
}
.register-link {
  width: 100%;
  margin: 12px 0 0;
}
.sso-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.primary-sso {
  margin-top: 20px;
}
.sso-btn {
  width: 100%;
  font-size: 15px;
}
.sso-icon {
  width: 18px;
  height: 18px;
  object-fit: contain;
  margin-right: 4px;
}
</style>
