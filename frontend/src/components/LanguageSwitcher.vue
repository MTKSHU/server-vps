<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { setAppLocale, type AppLocale } from "../i18n";

const { locale, t } = useI18n();

const currentLocale = computed({
  get: () => locale.value,
  set: (value: string) => setAppLocale(value as AppLocale),
});
</script>

<template>
  <el-select
    v-model="currentLocale"
    class="language-switcher"
    :aria-label="t('language.label')"
    size="small"
  >
    <el-option :label="t('language.zhCN')" value="zh-CN" />
    <el-option :label="t('language.enUS')" value="en-US" />
  </el-select>
</template>

<style scoped>
.language-switcher {
  width: 118px;
}
</style>
