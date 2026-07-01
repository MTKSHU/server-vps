<script setup lang="ts">
import { useI18n } from "vue-i18n";

const props = defineProps<{ value: string }>();
const { t, te } = useI18n();

function tagType(value: string) {
  if (["online", "running", "used", "enabled", "succeeded", "ready"].includes(value)) return "success";
  if (["pending", "planned", "verifying", "retrying", "provisioning", "stopped", "starting", "stopping", "restarting", "deleting", "disabled"].includes(value)) return "warning";
  if (["offline", "expired", "failed", "missing"].includes(value)) return "danger";
  return "info";
}

function statusLabel(value: string) {
  const key = `status.${value}`;
  return te(key) ? t(key) : value;
}
</script>

<template>
  <el-tag :type="tagType(props.value)" effect="light" round>{{ statusLabel(props.value) }}</el-tag>
</template>
