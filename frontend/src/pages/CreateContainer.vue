<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Delete, Plus, VideoPlay } from "@element-plus/icons-vue";
import {
  createContainer,
  getImages,
  getNodes,
  getMe,
  getUserPreference,
  type Gpu,
  type Image,
  type Node,
} from "../api/cluster";
import { hasAdminAccess } from "../auth";

const router = useRouter();
const props = withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false });
const emit = defineEmits<{ created: [] }>();
const images = ref<Image[]>([]);
const nodes = ref<Node[]>([]);
const myQuota = ref({ cpu_cores: 1, memory_gb: 1, disk_gb: 20, container_disk_limit_gb: 500, storage_quota_gb: 500, gpu_count: 0, container_count: 1 });
const myUsage = ref<Record<string, number>>({});
const myAllowedNodeIds = ref<number[] | null>(null);
const submitting = ref(false);
type PortProtocol = "tcp" | "udp" | "both";
type PortType = "ssh" | "web" | "custom";
type PortFormRow = { name: string; port_type: PortType; protocol: PortProtocol; container_port: number };
const ports = ref<PortFormRow[]>([]);

function randomSuffix() {
  return Math.random().toString(36).slice(2, 6);
}

function generateName(hostname: string) {
  const base = hostname.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-+|-+$/g, "").slice(0, 26);
  const safe = /^[a-z]/.test(base) ? base : `c-${base}`.slice(0, 26);
  return `${safe}-${randomSuffix()}`;
}

const form = reactive({
  name: "",
  image_id: "",
  node_id: undefined as number | undefined,
  cpu_cores: 8,
  memory_gb: 32,
  gpu_count: 1,
  gpu_ids: [] as number[],
  ssh_username: "ubuntu",
});

const selectedImage = computed(() => images.value.find((image) => image.id === form.image_id));
const selectedNode = computed(() => nodes.value.find((node) => node.id === form.node_id));
const isAdmin = computed(() => hasAdminAccess());

const orderedNodes = computed(() => {
  if (isAdmin.value) return nodes.value;
  const allowed = new Set(myAllowedNodeIds.value || []);
  const prioritized = nodes.value.filter((node) => allowed.has(node.id));
  const rest = nodes.value.filter((node) => !allowed.has(node.id));
  return [...prioritized, ...rest];
});

function freeCpuCores(node: Node) {
  return Math.max(0, node.cpu_total - node.cpu_used);
}

function freeMemoryGb(node: Node) {
  return Math.max(0, node.memory_total_gb - node.reserved_memory_gb - node.memory_used_gb);
}

function freeDiskGb(node: Node) {
  return Math.max(0, node.disk_total_gb - node.reserved_disk_gb - node.disk_used_gb);
}

function effectiveLimit(value: number, fallback: number) {
  return value > 0 ? value : fallback;
}

function userDiskLimit() {
  const remaining = Math.max(0, myQuota.value.disk_gb - (myUsage.value.disk_gb || 0));
  const perContainer = myQuota.value.container_disk_limit_gb > 0 ? myQuota.value.container_disk_limit_gb : remaining;
  return Math.max(0, Math.min(remaining, perContainer));
}

const selectableNodes = computed(() => {
  const pools = selectedImage.value?.compatible_pools.split(",").map((pool) => pool.trim()) || [];
  return nodes.value.filter((node) => {
    if (myAllowedNodeIds.value && !myAllowedNodeIds.value.includes(node.id)) return false;
    return (
      node.status === "online" &&
      node.schedulable &&
      !node.maintenance &&
      node.node_type !== "storage" &&
      node.incus_status !== "unavailable" && node.incus_status !== "" &&
      pools.includes(node.driver_pool) &&
      freeCpuCores(node) >= 1 &&
      freeMemoryGb(node) >= 1 &&
      freeDiskGb(node) >= 20
    );
  });
});

const nodeGpus = computed<Gpu[]>(() => {
  return selectedNode.value?.gpus || [];
});

const schedulableGpus = computed(() => {
  return nodeGpus.value;
});

function gpuActiveCount(gpu: Gpu) {
  return gpu.containers?.length || (gpu.container ? 1 : 0);
}

function gpuShareLimit() {
  if (!selectedNode.value) return 0;
  return selectedNode.value.allow_gpu_sharing ? selectedNode.value.max_gpu_shared_containers : 1;
}

function isGpuAvailable(gpu: Gpu) {
  const limit = gpuShareLimit();
  return limit > 0 && gpuActiveCount(gpu) < limit;
}

function gpuOptionLabel(gpu: Gpu) {
  const limit = gpuShareLimit();
  const usage = limit ? `${gpuActiveCount(gpu)}/${limit}` : `${gpuActiveCount(gpu)}`;
  return `GPU ${gpu.slot} · ${gpu.model} · ${usage}`;
}

const maxSelectableGpuCount = computed(() => {
  const availableCount = schedulableGpus.value.filter(isGpuAvailable).length;
  const quotaLimit = isAdmin.value ? 8 : Math.max(0, myQuota.value.gpu_count - (myUsage.value.gpu_count || 0));
  return Math.max(0, Math.min(availableCount, quotaLimit, 8));
});

const gpuModelSummary = computed(() => {
  const counts = new Map<string, number>();
  for (const gpu of nodeGpus.value) {
    counts.set(gpu.model, (counts.get(gpu.model) || 0) + 1);
  }
  return Array.from(counts.entries())
    .map(([model, count]) => `${model} x ${count}`)
    .join("，");
});

const maxCpuCores = computed(() =>
  selectedNode.value
    ? Math.max(1, Math.min(freeCpuCores(selectedNode.value), effectiveLimit(selectedNode.value.max_cpu_per_container, selectedNode.value.cpu_total)))
    : myQuota.value.cpu_cores
);
const maxMemoryGb = computed(() =>
  selectedNode.value
    ? Math.max(
        1,
        Math.min(
          freeMemoryGb(selectedNode.value),
          effectiveLimit(selectedNode.value.max_memory_gb_per_container, selectedNode.value.memory_total_gb)
        )
      )
    : myQuota.value.memory_gb
);
const maxWorkspaceGb = computed(() =>
  selectedNode.value
    ? Math.max(
        0,
        Math.min(
          freeDiskGb(selectedNode.value),
          effectiveLimit(selectedNode.value.max_disk_gb_per_container, selectedNode.value.disk_total_gb),
          userDiskLimit()
        )
      )
    : Math.max(0, userDiskLimit())
);

// 只读：workspace 卷大小信息
const workspaceGbText = computed(() => {
  const gb = maxWorkspaceGb.value;
  return gb > 0 ? `${gb} GB（根据用户配额和节点限制自动计算）` : "节点可用磁盘自动分配";
});

const ROOT_DISK_GB = 50; // 与后端 CONTAINER_ROOT_DISK_GB 一致

const nodeSummary = computed(() => {
  if (!selectedNode.value) return "";
  const node = selectedNode.value;
  const gpuText = gpuModelSummary.value || "无 GPU";
  return `GPU ${schedulableGpus.value.length}/${node.gpus.length} 可共享（${gpuText}），CPU ${maxCpuCores.value}/${node.cpu_total} 核可用，内存 ${maxMemoryGb.value}/${node.memory_total_gb} GB 可用`;
});

function clampResourceForm() {
  form.cpu_cores = Math.min(Math.max(form.cpu_cores, 1), maxCpuCores.value);
  form.memory_gb = Math.min(Math.max(form.memory_gb, 1), maxMemoryGb.value);
  const validIds = new Set(schedulableGpus.value.filter(isGpuAvailable).map((gpu) => gpu.id));
  form.gpu_ids = form.gpu_ids.filter((id) => validIds.has(id)).slice(0, maxSelectableGpuCount.value);
  form.gpu_count = form.gpu_ids.length;
}

function applyResourceDefaults() {
  form.cpu_cores = maxCpuCores.value;
  form.memory_gb = Math.max(1, Math.floor(maxMemoryGb.value / 2));
  form.gpu_ids = schedulableGpus.value.filter(isGpuAvailable).slice(0, maxSelectableGpuCount.value).map((gpu) => gpu.id);
  form.gpu_count = form.gpu_ids.length;
  clampResourceForm();
}

function applyPortType(row: PortFormRow) {
  if (row.port_type === "ssh") {
    row.name = "ssh";
    row.container_port = 22;
    row.protocol = "tcp";
  } else if (row.port_type === "web") {
    row.name = row.name || "web";
    row.container_port = row.container_port === 22 ? 80 : row.container_port;
    row.protocol = row.protocol === "udp" ? "tcp" : row.protocol;
  }
}

function expandedPorts() {
  return ports.value.flatMap((port) => {
    const base = { name: port.name, container_port: port.container_port };
    if (port.protocol === "both") {
      return [
        { ...base, protocol: "tcp" },
        { ...base, protocol: "udp" }
      ];
    }
    return [{ ...base, protocol: port.protocol }];
  });
}

async function load() {
  const [imageRows, nodeRows, me, favorites] = await Promise.all([
    getImages(),
    getNodes(),
    getMe(),
    getUserPreference<{ image_ids?: string[] }>("image_favorites").catch(() => ({ value: { image_ids: [] } })),
  ]);
  const favoriteIds = new Set(Array.isArray(favorites.value?.image_ids) ? favorites.value.image_ids : []);
  images.value = [...imageRows].sort((a, b) => {
    const favoriteRank = Number(favoriteIds.has(b.id)) - Number(favoriteIds.has(a.id));
    if (favoriteRank) return favoriteRank;
    return a.name.localeCompare(b.name, "zh");
  });
  nodes.value = nodeRows; myQuota.value = me.quota; myUsage.value = me.usage;
  myAllowedNodeIds.value = Array.isArray(me.allowed_node_ids) ? me.allowed_node_ids : null;
  form.image_id = images.value[0]?.id || "";
  const firstNode = selectableNodes.value[0];
  form.node_id = firstNode?.id;
  if (firstNode) form.name = generateName(firstNode.hostname);
  applyResourceDefaults();
}

function addPort() {
  const nextType: PortType = ports.value.some((port) => port.port_type === "ssh") ? "web" : "ssh";
  const nextPort: PortFormRow = {
    name: nextType,
    port_type: nextType,
    protocol: "tcp",
    container_port: nextType === "ssh" ? 22 : 80
  };
  if (selectedNode.value && !selectedNode.value.allow_port_mapping) {
    ElMessage.error("当前节点不允许端口映射");
    return;
  }
  if (selectedNode.value && expandedPorts().length + 1 > selectedNode.value.max_ports_per_container) {
    ElMessage.error(`当前节点单容器最多允许 ${selectedNode.value.max_ports_per_container} 个端口映射`);
    return;
  }
  ports.value.push(nextPort);
}

function removePort(index: number) {
  ports.value.splice(index, 1);
}

async function submit() {
  if (!form.node_id || !selectableNodes.value.some((node) => node.id === form.node_id)) {
    ElMessage.error("请选择节点");
    return;
  }
  clampResourceForm();
  if (form.gpu_ids.length > maxSelectableGpuCount.value) {
    ElMessage.error(`当前条件下最多可选 ${maxSelectableGpuCount.value} 张 GPU`);
    return;
  }
  if (selectedNode.value && expandedPorts().length > selectedNode.value.max_ports_per_container) {
    ElMessage.error(`当前节点单容器最多允许 ${selectedNode.value.max_ports_per_container} 个端口映射`);
    return;
  }
  submitting.value = true;
  try {
    await createContainer({
      name: form.name,
      image_id: form.image_id,
      node_id: form.node_id,
      cpu_cores: form.cpu_cores,
      memory_gb: form.memory_gb,
      disk_gb: ROOT_DISK_GB,
      gpu_count: form.gpu_ids.length,
      gpu_ids: form.gpu_ids,
      ssh_username: form.ssh_username,
      ports: expandedPorts(),
      resources: [],
      expires_at: 0,
    });
    ElMessage.success("容器已创建");
    if (props.embedded) {
      emit("created");
    } else {
      router.push({ name: "containers" });
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "创建失败");
  } finally {
    submitting.value = false;
  }
}

onMounted(load);

watch([selectedNode, selectedImage], () => {
  if (selectableNodes.value.length && !selectableNodes.value.some((node) => node.id === form.node_id)) {
    const node = selectableNodes.value[0];
    form.node_id = node.id;
    form.name = generateName(node.hostname);
  } else if (!selectableNodes.value.length) {
    form.node_id = undefined;
  }
  applyResourceDefaults();
});

watch(
  () => form.gpu_ids,
  () => {
    form.gpu_count = form.gpu_ids.length;
  },
  { deep: true }
);

watch(
  () => form.node_id,
  (newId) => {
    const node = nodes.value.find((n) => n.id === newId);
    if (node) form.name = generateName(node.hostname);
  }
);

// ── 选择 code-server / jupyterlab 镜像时自动预填 Web 端口 ────────────────────
const IMAGE_WEB_PRESETS: Record<string, { name: string; container_port: number }> = {
  "code-server": { name: "code-server", container_port: 8080 },
  jupyterlab:    { name: "jupyterlab",  container_port: 8888 },
};

function detectWebPreset(imageId: string) {
  const lower = imageId.toLowerCase();
  for (const [key, preset] of Object.entries(IMAGE_WEB_PRESETS)) {
    if (lower.includes(key)) return preset;
  }
  return null;
}

watch(
  () => form.image_id,
  (newId) => {
    if (!newId) return;
    const preset = detectWebPreset(newId);

    // 移除之前由本 watch 自动添加的 web 端口（保留用户手动添加的）
    const allPresetNames = new Set(Object.values(IMAGE_WEB_PRESETS).map((p) => p.name));
    ports.value = ports.value.filter((p) => !allPresetNames.has(p.name));

    if (!preset) return;

    // 确保 SSH 端口存在
    if (!ports.value.some((p) => p.container_port === 22 && p.protocol === "tcp")) {
      ports.value.push({ name: "ssh", port_type: "ssh", protocol: "tcp", container_port: 22 });
    }
    // 追加 web 端口
    ports.value.push({ name: preset.name, port_type: "web", protocol: "tcp", container_port: preset.container_port });
  }
);
</script>

<template>
  <el-card shadow="never">
    <template #header><strong>创建 GPU Linux 容器</strong></template>
    <el-form :model="form" label-position="top" class="form-grid">
      <el-form-item label="节点">
        <el-select v-model="form.node_id" filterable>
          <el-option
            v-for="node in orderedNodes"
            :key="node.id"
            :label="`${node.hostname} · ${node.driver_pool}`"
            :value="node.id"
            :disabled="!selectableNodes.some((item) => item.id === node.id)"
          />
        </el-select>
        <small v-if="nodeSummary" class="field-hint">{{ nodeSummary }}</small>
        <small v-if="selectedNode?.cuda_driver_api_version" class="field-hint">最大支持 CUDA {{ selectedNode.cuda_driver_api_version }}</small>
      </el-form-item>
      <el-form-item label="镜像">
        <el-select v-model="form.image_id">
          <el-option v-for="image in images" :key="image.id" :label="image.name" :value="image.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="容器名称">
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item label="CPU 核数">
        <el-input-number v-model="form.cpu_cores" :min="1" :max="maxCpuCores" />
      </el-form-item>
      <el-form-item label="内存 GB">
        <el-input-number v-model="form.memory_gb" :min="1" :max="maxMemoryGb" />
      </el-form-item>
      <el-form-item label="Root Disk (/)">
        <el-input :value="`${ROOT_DISK_GB} GB（固定）`" disabled />
        <small class="field-hint">容器系统盘，固定 {{ ROOT_DISK_GB }} GB</small>
      </el-form-item>
      <el-form-item label="数据卷 /workspace">
        <el-input :value="workspaceGbText" disabled />
        <small class="field-hint">持久数据卷，同节点上为此用户的所有容器共享复用</small>
      </el-form-item>
      <el-form-item label="GPU 编号">
        <el-select v-model="form.gpu_ids" multiple collapse-tags collapse-tags-tooltip :multiple-limit="maxSelectableGpuCount">
          <el-option label="不使用 GPU" :value="0" disabled />
          <el-option
            v-for="gpu in schedulableGpus"
            :key="gpu.id"
            :value="gpu.id"
            :label="gpuOptionLabel(gpu)"
            :disabled="!isGpuAvailable(gpu)"
          />
        </el-select>
        <small class="field-hint">已选择 {{ form.gpu_ids.length }} / {{ maxSelectableGpuCount }} 张 GPU</small>
      </el-form-item>
      <el-form-item label="初始用户名">
        <el-input v-model="form.ssh_username" />
      </el-form-item>

    </el-form>
    <el-divider />
    <div class="card-header">
      <strong>端口映射</strong>
      <el-button :icon="Plus" @click="addPort">添加映射</el-button>
    </div>
    <el-table :data="ports" style="margin: 12px 0 18px">
      <el-table-column label="名称" min-width="160">
        <template #default="{ row }"><el-input v-model="row.name" placeholder="ssh / jupyter / web" /></template>
      </el-table-column>
      <el-table-column label="端口类型" width="150">
        <template #default="{ row }">
          <el-select v-model="row.port_type" @change="applyPortType(row)">
            <el-option label="SSH" value="ssh" />
            <el-option label="Web" value="web" />
            <el-option label="自定义" value="custom" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="容器内端口" width="180">
        <template #default="{ row }"><el-input-number v-model="row.container_port" :min="1" :max="65535" /></template>
      </el-table-column>
      <el-table-column label="协议" width="150">
        <template #default="{ row }">
          <el-select v-model="row.protocol">
            <el-option label="TCP" value="tcp" />
            <el-option label="UDP" value="udp" />
            <el-option label="BOTH" value="both" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="平台入口 / 节点转发" min-width="180">
        <template #default>创建时自动分配</template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="{ $index }"><el-button type="danger" :icon="Delete" @click="removePort($index)">删除</el-button></template>
      </el-table-column>
    </el-table>
    <el-button type="primary" :icon="VideoPlay" :loading="submitting" @click="submit">创建并启动</el-button>
  </el-card>
</template>
