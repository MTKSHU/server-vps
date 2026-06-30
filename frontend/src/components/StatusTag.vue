<script setup lang="ts">
const props = defineProps<{ value: string }>();

function tagType(value: string) {
  if (["online", "running", "used", "enabled", "succeeded", "ready"].includes(value)) return "success";
  if (["pending", "planned", "verifying", "retrying", "provisioning", "stopped", "starting", "stopping", "restarting", "deleting", "disabled"].includes(value)) return "warning";
  if (["offline", "expired", "failed", "missing"].includes(value)) return "danger";
  return "info";
}
</script>

<template>
  <el-tag :type="tagType(props.value)" effect="light" round>{{ props.value }}</el-tag>
</template>
