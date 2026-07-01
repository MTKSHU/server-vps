<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Back, Plus } from "@element-plus/icons-vue";
import { getAuthConfig, registerPlatformUser, type AuthConfig } from "../api/cluster";
import LanguageSwitcher from "../components/LanguageSwitcher.vue";

const router = useRouter();
const { t } = useI18n();
const loading = ref(false);
const config = ref<AuthConfig | null>(null);
const form = reactive({
  username: "",
  email: "",
  password: "",
  confirmPassword: "",
});

onMounted(async () => {
  try {
    config.value = await getAuthConfig();
  } catch {
    config.value = null;
  }
});

async function submit() {
  if (config.value && (!config.value.registration_enabled || config.value.registration_mode !== "platform")) {
    ElMessage.error(t("auth.platformRegisterDisabled"));
    return;
  }
  if (form.password !== form.confirmPassword) {
    ElMessage.error(t("auth.passwordMismatch"));
    return;
  }
  loading.value = true;
  try {
    const result = await registerPlatformUser({
      username: form.username,
      email: form.email,
      password: form.password,
    });
    ElMessage.success(result.enabled ? t("auth.registerSuccessLogin") : t("auth.registerSuccessPending"));
    await router.replace({ name: "platformLogin" });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t("auth.registerFailed"));
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="login-card-toolbar">
        <LanguageSwitcher />
      </div>
      <h1>{{ t("app.name") }}</h1>
      <p>{{ t("auth.registering") }}</p>

      <el-form :model="form" label-position="top" class="register-form" @submit.prevent="submit">
        <el-form-item :label="t('auth.username')">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item :label="t('auth.email')">
          <el-input v-model="form.email" autocomplete="email" />
        </el-form-item>
        <el-form-item :label="t('auth.password')">
          <el-input v-model="form.password" type="password" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item :label="t('auth.confirmPassword')">
          <el-input v-model="form.confirmPassword" type="password" show-password autocomplete="new-password" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" class="register-submit" :icon="Plus" :loading="loading" data-keep-label="true" @click="submit">
          {{ t("common.register") }}
        </el-button>
        <el-button class="back-login" :icon="Back" data-keep-label="true" @click="router.push({ name: 'platformLogin' })">
          {{ t("auth.backToLogin") }}
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login-card-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
.register-form {
  margin-top: 20px;
}
.register-submit,
.back-login {
  width: 100%;
}
.back-login {
  margin: 12px 0 0;
}
</style>
