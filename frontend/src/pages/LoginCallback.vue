<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { ssoCallback } from "../api/cluster";
import { setAuth, type AuthUser } from "../auth";

const router = useRouter();
const statusText = ref("正在完成统一认证，请稍候…");

onMounted(async () => {
  const params = new URLSearchParams(window.location.search);
  const state = params.get("state") || "";
  const code = params.get("code") || "";
  const ticket = params.get("ticket") || "";

  if (!state) {
    statusText.value = "认证参数缺失，即将返回登录页…";
    await delay(2000);
    await router.replace({ name: "login" });
    return;
  }

  try {
    const result = await ssoCallback({ state, code, ticket });
    setAuth(result.token, result.user as AuthUser);
    await router.replace({ name: "dashboard" });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "认证失败";
    ElMessage.error(msg);
    statusText.value = `${msg}，即将返回登录页…`;
    await delay(3000);
    await router.replace({ name: "login" });
  }
});

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
</script>

<template>
  <div class="sso-callback-page">
    <el-card class="sso-card" shadow="always">
      <el-icon class="spin-icon"><Loading /></el-icon>
      <p>{{ statusText }}</p>
    </el-card>
  </div>
</template>

<style scoped>
.sso-callback-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.sso-card {
  width: 360px;
  text-align: center;
  padding: 40px 24px;
}
.spin-icon {
  font-size: 48px;
  color: var(--el-color-primary);
  animation: spin 1s linear infinite;
  margin-bottom: 16px;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
