<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Plus, Promotion } from "@element-plus/icons-vue";
import { login, getAuthConfig, getSSOProviders, ssoStartUrl, type AuthConfig, type SSOProvider } from "../api/cluster";
import { setAuth } from "../auth";
import LanguageSwitcher from "../components/LanguageSwitcher.vue";

const router = useRouter();
const route = useRoute();
const { t } = useI18n();
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
    ElMessage.error(t("auth.platformLoginDisabled"));
    return;
  }
  loading.value = true;
  try {
    const result = await login(form.username, form.password);
    setAuth(result.token, result.user);
    await router.replace((route.query.redirect as string) || { name: "dashboard" });
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : t("auth.loginFailed"));
  } finally { loading.value = false; }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="login-card-toolbar">
        <LanguageSwitcher />
      </div>
      <h1>{{ t("app.name") }}</h1>
      <p>{{ t("auth.loginTitle") }}</p>

      <el-tabs v-model="activeTab" stretch class="login-tabs">
        <el-tab-pane :label="t('auth.ssoLogin')" name="sso">
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
              {{ p.display_name || t("auth.defaultSSOProvider") }}
            </el-button>
            <el-empty v-if="ssoProviders.length === 0" :description="t('auth.noSSOProvider')" />
          </div>
        </el-tab-pane>

        <el-tab-pane :label="t('auth.platformLogin')" name="platform">
          <el-form :model="form" label-position="top" class="login-form" @submit.prevent="submitLocal">
            <template v-if="!authConfig || authConfig.local_login_enabled">
              <el-form-item :label="t('auth.username')"><el-input v-model="form.username" autocomplete="username" /></el-form-item>
              <el-form-item :label="t('auth.password')"><el-input v-model="form.password" type="password" show-password autocomplete="current-password" @keyup.enter="submitLocal" /></el-form-item>
              <el-button type="primary" class="login-submit" :loading="loading" @click="submitLocal">
                {{ t("common.login") }}
              </el-button>
            </template>
            <el-empty v-else :description="t('auth.platformLoginDisabled')" />
            <el-button
              v-if="showPlatformRegister"
              class="register-link"
              :icon="Plus"
              data-keep-label="true"
              @click="router.push({ name: 'register' })"
            >
              {{ t("auth.registerPlatformAccount") }}
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.login-card-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
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
