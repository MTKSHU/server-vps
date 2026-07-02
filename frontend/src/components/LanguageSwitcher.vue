<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { Languages } from "lucide-vue-next";
import { setAppLocale, type AppLocale } from "../i18n";

const { locale } = useI18n();

const langLabel = computed(() => locale.value === "zh-CN" ? "中" : "EN");

function toggle() {
  setAppLocale(locale.value === "zh-CN" ? "en-US" : "zh-CN");
}
</script>

<template>
  <el-tooltip :content="locale === 'zh-CN' ? 'Switch to English' : '切换为中文'" placement="bottom">
    <button class="lang-btn" @click="toggle" :aria-label="locale === 'zh-CN' ? 'Switch to English' : '切换为中文'">
      <Languages :size="15" />
      <span class="lang-label">{{ langLabel }}</span>
    </button>
  </el-tooltip>
</template>

<style scoped>
.lang-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  background: transparent;
  color: var(--el-text-color-regular);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  transition: color 0.2s, border-color 0.2s, background 0.2s;
}
.lang-btn:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.lang-label {
  font-weight: 600;
  letter-spacing: 0.02em;
}
</style>
