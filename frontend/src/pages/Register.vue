<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Back, Plus } from "@element-plus/icons-vue";
import { getAuthConfig, registerPlatformUser, type AuthConfig } from "../api/cluster";

const router = useRouter();
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
    ElMessage.error("平台注册未启用");
    return;
  }
  if (form.password !== form.confirmPassword) {
    ElMessage.error("两次输入的密码不一致");
    return;
  }
  loading.value = true;
  try {
    const result = await registerPlatformUser({
      username: form.username,
      email: form.email,
      password: form.password,
    });
    ElMessage.success(result.enabled ? "注册成功，请登录" : "注册成功，等待管理员审核");
    await router.replace({ name: "platformLogin" });
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "注册失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <h1>GPU 集群平台</h1>
      <p>注册平台账号</p>

      <el-form :model="form" label-position="top" class="register-form" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="电子邮箱">
          <el-input v-model="form.email" autocomplete="email" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="确认密码">
          <el-input v-model="form.confirmPassword" type="password" show-password autocomplete="new-password" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" class="register-submit" :icon="Plus" :loading="loading" data-keep-label="true" @click="submit">
          注册
        </el-button>
        <el-button class="back-login" :icon="Back" data-keep-label="true" @click="router.push({ name: 'platformLogin' })">
          返回登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
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
