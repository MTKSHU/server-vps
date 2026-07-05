<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Close, Delete, Edit, Plus, Refresh, Select, Star, StarFilled, Upload } from "@element-plus/icons-vue";
import {
  deleteImage, deleteNodeImage, copyLocalImage, deleteStorageImage, distributeStorageImage, getImageCatalog, getNodes, getStorageImages,
  getUserPreference, updateUserPreference,
  getUbuntuRemoteImages, pullImageToNode, saveImage,
  type Image, type IncusImage, type Node, type StorageImageFile, type UbuntuRemoteImage
} from "../api/cluster";
import { authUser } from "../auth";

const { t } = useI18n();
const loading = ref(false);
const images = ref<Image[]>([]);
const nodeImages = ref<IncusImage[]>([]);
const computeNodes = ref<Node[]>([]);
const storedImages = ref<StorageImageFile[]>([]);
const favoriteImageIds = ref<string[]>([]);
const onlyMyStoredImages = ref(false);
const remoteImages = ref<UbuntuRemoteImage[]>([]);
const remoteLoading = ref(false);
const remoteLoaded = ref(false);
const remoteArch = ref("amd64");
const remoteVariant = ref("default");
const dialog = ref(false);
const editingId = ref("");
const form = reactive({ id:"", name:"", incus_ref:"", cuda_major:0, compatible_pools:"", owner:"admin", enabled:true, preferred:true });

function isRemoteRef(ref: string) { return ref.includes(":") && !ref.startsWith("local:") && ref.split(":").slice(1).join(":").includes("/"); }
function localAlias(ref: string) { return ref.includes(":") ? ref.split(":").slice(1).join(":") : ref; }
function nodeHasImage(nodeId: number, incus_ref: string): boolean {
  const alias = localAlias(incus_ref);
  return nodeImages.value.some(ni => {
    if (ni.node_id !== nodeId) return false;
    if (ni.fingerprint.startsWith(alias) || ni.fingerprint.startsWith(incus_ref)) return true;
    return ni.aliases.split(",").map(a => a.trim()).filter(Boolean).some(a => a === alias || a === incus_ref);
  });
}
function mountedCount(image: Image) { return computeNodes.value.filter(n => nodeHasImage(n.id, image.incus_ref)).length; }

const displayedRemoteImages = computed(() =>
  remoteImages.value.filter(row =>
    (remoteArch.value === "" || row.arch === remoteArch.value) &&
    (remoteVariant.value === "" || row.variant === remoteVariant.value)
  )
);
const favoriteImages = computed(() => images.value.filter(image => favoriteImageIds.value.includes(image.id)));
const storedImagesForDisplay = computed(() => {
  if (!onlyMyStoredImages.value) return storedImages.value;
  return storedImages.value.filter(row => row.owner_id === authUser.value?.id || row.owner === authUser.value?.username);
});
const remoteArchOptions = computed(() => [...new Set(remoteImages.value.map(r => r.arch))].sort());
const remoteVariantOptions = computed(() => [...new Set(remoteImages.value.map(r => r.variant))].sort());

async function load() {
  loading.value = true;
  try {
    const [catalog, storage, nodes, favorites] = await Promise.all([
      getImageCatalog(),
      getStorageImages(),
      getNodes(),
      getUserPreference<{ image_ids?: string[] }>("image_favorites").catch(() => ({ value: { image_ids: [] } }))
    ]);
    images.value = catalog.images;
    nodeImages.value = catalog.incus_images;
    storedImages.value = storage.files;
    favoriteImageIds.value = Array.isArray(favorites.value?.image_ids) ? favorites.value.image_ids : [];
    computeNodes.value = nodes.filter(n => ["compute", "mixed"].includes(n.node_type) && n.status === "online");
  } finally { loading.value = false; }
}
async function loadRemote() {
  remoteLoading.value = true;
  try { remoteImages.value = await getUbuntuRemoteImages(); remoteLoaded.value = true; }
  catch (e: any) { ElMessage.error(e?.message || t("images.remoteLoadFailed")); }
  finally { remoteLoading.value = false; }
}
function open(row?: Image) {
  editingId.value = row?.id || "";
  Object.assign(form, row || { id:"", name:"", incus_ref:"", cuda_major:0, compatible_pools:"", owner:"admin", enabled:true, preferred:true });
  dialog.value = true;
}
function registerRemote(row: UbuntuRemoteImage) {
  const ref = row.incus_ref;
  const label = row.version ? `Ubuntu ${row.version}` : `Ubuntu ${row.release}`;
  const id = ref.replace(/[^a-zA-Z0-9_.:-]/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
  editingId.value = "";
  Object.assign(form, { id, name: `${label} (${row.arch}${row.variant !== "default" ? "/" + row.variant : ""})`, incus_ref: ref, cuda_major: 0, compatible_pools: "", owner: "admin", enabled: true, preferred: true });
  dialog.value = true;
}
function registerStored(row: StorageImageFile) {
  const ref = row.alias || row.aliases.split(",")[0]?.trim() || row.fingerprint;
  const id = ref.replace(/[^a-zA-Z0-9_.:-]/g, "-").replace(/^-+|-+$/g, "").toLowerCase();
  editingId.value = "";
  Object.assign(form, { id, name: row.description || ref, incus_ref: ref, cuda_major: 0, compatible_pools: "", owner: "admin", enabled: true, preferred: true });
  dialog.value = true;
}
async function removeStoredImage(row: StorageImageFile) {
  try {
    await ElMessageBox.confirm(
      t("images.confirmRemoveStored", { name: row.alias || row.aliases }),
      t("images.confirmRemoveStoredTitle"),
      { confirmButtonText: t("images.remove"), cancelButtonText: t("common.cancel"), type: "warning" }
    );
  } catch { return; }
  try {
    await deleteStorageImage(row.id);
    ElMessage.success(t("images.storedRemoved"));
    await load();
  } catch (e: any) {
    ElMessage.error(e?.message || e?.detail || t("images.removeFailed"));
  }
}
async function mountToNode(image: Image, nodeId: number) {
  try {
    if (isRemoteRef(image.incus_ref)) {
      // 远程镜像（如 images:ubuntu/24.04）：直接让目标节点 incus image copy 拉取
      await pullImageToNode(image.incus_ref, nodeId);
      ElMessage.success(t("images.pullTaskSubmitted"));
    } else {
      // 本地自建镜像（如 local:cluster/code-server）：
      // 先看是否有已导出的存储文件，有则直接分发；
      // 没有则通过管理端 SSH 在节点间 export→transfer→import
      const alias = localAlias(image.incus_ref);
      const sf = storedImages.value.find(s =>
        s.alias === alias || s.alias === image.incus_ref ||
        s.aliases.split(",").map(a => a.trim()).some(a => a === alias || a === image.incus_ref)
      );
      if (sf && sf.status === "exported") {
        await distributeStorageImage(sf.id, [nodeId]);
        ElMessage.success(t("images.distributeTaskSubmitted"));
      } else {
        // SSH pipeline：源节点 export → 管理端中转 → 目标节点 import
        const result = await copyLocalImage(image.incus_ref, nodeId);
        ElMessage.success(result.message || t("images.transferStarted"));
      }
    }
    await load();
  } catch (e: any) { ElMessage.error(e?.message || t("images.operationFailed")); }
}
async function submit() { await saveImage(form); dialog.value = false; ElMessage.success(t("images.catalogSaved")); await load(); }
async function remove(row: Image) { await ElMessageBox.confirm(t("images.confirmRemoveImage", { name: row.name }), t("images.removeImageTitle"), {type:"warning", confirmButtonText: t("images.remove"), cancelButtonText: t("common.cancel")}); await deleteImage(row.id); await load(); }
async function toggleFavorite(row: Image) {
  const ids = new Set(favoriteImageIds.value);
  if (ids.has(row.id)) ids.delete(row.id);
  else ids.add(row.id);
  favoriteImageIds.value = Array.from(ids);
  await updateUserPreference("image_favorites", { image_ids: favoriteImageIds.value });
}

// ── 节点本地 Incus 镜像组（按 fingerprint 去重）────────────────────────────────
interface NodeImageGroup {
  fingerprint: string;
  aliases: string;
  description: string;
  architecture: string;
  updated_at: number;
  nodes: Array<{ id: number; hostname: string; status: string }>;
  primaryAlias: string;
}

const nodeImageGroups = computed<NodeImageGroup[]>(() => {
  const groups = new Map<string, NodeImageGroup>();
  for (const img of nodeImages.value) {
    const nodeEntry = { id: img.node_id, hostname: img.node, status: img.node_status };
    const existing = groups.get(img.fingerprint);
    if (existing) {
      if (!existing.nodes.some(n => n.id === img.node_id)) existing.nodes.push(nodeEntry);
    } else {
      const aliasList = (img.aliases || "").split(",").map(a => a.trim()).filter(Boolean);
      const primaryAlias = aliasList[0] || img.fingerprint.slice(0, 12);
      groups.set(img.fingerprint, {
        fingerprint: img.fingerprint,
        aliases: img.aliases,
        description: img.description,
        architecture: img.architecture,
        updated_at: img.updated_at,
        nodes: [nodeEntry],
        primaryAlias,
      });
    }
  }
  return Array.from(groups.values()).sort((a, b) => a.primaryAlias.localeCompare(b.primaryAlias));
});

function isNodeImageRegistered(group: NodeImageGroup): boolean {
  return images.value.some(
    img => img.incus_ref === `local:${group.primaryAlias}` || img.incus_ref === group.primaryAlias
  );
}

function registerFromNode(group: NodeImageGroup) {
  const incusRef = `local:${group.primaryAlias}`;
  const id = group.primaryAlias
    .replace(/[^a-zA-Z0-9_.:-]/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  // 从已有节点推断 compatible_pools
  const pools = [...new Set(
    group.nodes
      .map(n => computeNodes.value.find(cn => cn.id === n.id)?.driver_pool)
      .filter((p): p is string => Boolean(p))
  )].join(",");
  editingId.value = "";
  Object.assign(form, {
    id,
    name: group.description || group.primaryAlias,
    incus_ref: incusRef,
    cuda_major: 0,
    compatible_pools: pools,
    owner: "admin",
    enabled: true,
    preferred: true,
  });
  dialog.value = true;
}

// ── 节点本地镜像 ─ 移除 ───────────────────────────────────────────────────────
async function removeFromNode(group: NodeImageGroup, node: { id: number; hostname: string; status: string }) {
  try {
    await ElMessageBox.confirm(
      t("images.confirmRemoveFromNode", { node: node.hostname, alias: group.primaryAlias }),
      t("images.removeFromNodeTitle"),
      { type: "warning", confirmButtonText: t("images.remove"), cancelButtonText: t("common.cancel") }
    );
  } catch { return; }
  try {
    await deleteNodeImage(node.id, group.primaryAlias);
    ElMessage.success(t("images.removeFromNodeSubmitted"));
    await load();
  } catch (e: any) {
    ElMessage.error(e?.message || t("images.operationFailed"));
  }
}

// ── 节点本地镜像 ─ 分发到其他节点 ───────────────────────────────────────────────
async function distributeNodeImageToNode(group: NodeImageGroup, nodeId: number) {
  const alias = group.primaryAlias;
  const incusRef = `local:${alias}`;
  try {
    const sf = storedImages.value.find(s =>
      s.alias === alias || s.alias === incusRef ||
      s.aliases.split(",").map(a => a.trim()).some(a => a === alias || a === incusRef)
    );
    if (sf && sf.status === "exported") {
      await distributeStorageImage(sf.id, [nodeId]);
      ElMessage.success(t("images.distributeTaskSubmitted"));
    } else {
      const result = await copyLocalImage(incusRef, nodeId);
      ElMessage.success(result.message || t("images.transferStarted"));
    }
    await load();
  } catch (e: any) { ElMessage.error(e?.message || t("images.operationFailed")); }
}

// ── 平台镜像 ─ 指纹检测 ─────────────────────────────────────────────────────
function nodeImageFingerprint(nodeId: number, incus_ref: string): string {
  const alias = localAlias(incus_ref);
  const ni = nodeImages.value.find(img => {
    if (img.node_id !== nodeId) return false;
    return img.aliases.split(",").map(a => a.trim()).filter(Boolean)
      .some(a => a === alias || a === incus_ref);
  });
  return ni?.fingerprint || "";
}

// 所有已同步节点中 updated_at 最新的指纹（编制时间最近）
function latestFingerprint(image: Image): string {
  const alias = localAlias(image.incus_ref);
  const candidates = nodeImages.value
    .filter(img =>
      img.aliases.split(",").map(a => a.trim()).filter(Boolean)
        .some(a => a === alias || a === image.incus_ref)
    )
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  return candidates[0]?.fingerprint || "";
}

// 该节点的指纹是否与最新版本一致
function isNodeFingerprintLatest(nodeId: number, image: Image): boolean {
  const fp = nodeImageFingerprint(nodeId, image.incus_ref);
  if (!fp) return true;
  const latest = latestFingerprint(image);
  return !latest || fp === latest;
}

onMounted(() => { load(); loadRemote(); });
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <el-card shadow="never">
      <el-tabs>
        <el-tab-pane :label="t('images.platformImages')">
          <div class="card-header"><div><el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button></div></div>
          <el-table :data="images" stripe row-key="id">
            <el-table-column type="expand">
              <template #default="{row}">
                <div v-if="computeNodes.length === 0" style="padding:12px 24px;color:#999">{{ t("images.noOnlineComputeNodes") }}</div>
                <div v-else style="padding:8px 24px;display:flex;flex-wrap:wrap;gap:8px">
                  <div v-for="n in computeNodes" :key="n.id" style="display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid #e4e7ed;border-radius:4px;background:#fafafa">
                    <span style="font-size:13px">{{ n.hostname }}</span>
                    <template v-if="nodeHasImage(n.id, row.incus_ref)">
                      <el-tag :type="isNodeFingerprintLatest(n.id, row) ? 'success' : 'warning'" size="small">
                        {{ isNodeFingerprintLatest(n.id, row) ? t("images.synced") : t("images.outdated") }}
                      </el-tag>
                      <span style="font-size:11px;font-family:monospace;color:var(--el-text-color-secondary)" :title="nodeImageFingerprint(n.id, row.incus_ref)">
                        {{ nodeImageFingerprint(n.id, row.incus_ref).slice(0, 8) }}
                      </span>
                      <el-button
                        v-if="!isNodeFingerprintLatest(n.id, row)"
                        size="small" type="warning" plain :icon="Refresh"
                        @click="mountToNode(row, n.id)">
                        {{ t("images.resync") }}
                      </el-button>
                    </template>
                    <el-button v-else size="small" type="primary" plain :icon="Upload" @click="mountToNode(row, n.id)">{{ t("images.sync") }}</el-button>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="name" :label="t('images.name')" min-width="160"/>
            <el-table-column prop="incus_ref" :label="t('images.incusRef')" min-width="200"/>
            <el-table-column prop="cuda_major" label="CUDA" width="80"/>
            <el-table-column :label="t('images.favorite')" width="70">
              <template #default="{row}">
                <el-button link type="primary" :icon="favoriteImageIds.includes(row.id) ? StarFilled : Star" @click="toggleFavorite(row)" />
              </template>
            </el-table-column>
            <el-table-column :label="t('images.nodeSync')" width="90">
              <template #default="{row}">
                <el-tag size="small" :type="computeNodes.length > 0 && mountedCount(row) === computeNodes.length ? 'success' : 'info'">
                  {{ mountedCount(row) }}/{{ computeNodes.length }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('images.enabled')" width="70"><template #default="{row}">{{ row.enabled?t("images.yes"):t("images.no") }}</template></el-table-column>
            <el-table-column :label="t('images.actions')" width="200" fixed="right"><template #default="{row}"><el-button size="small" :icon="Edit" @click="open(row)">{{ t("common.edit") }}</el-button><el-button size="small" type="danger" :icon="Delete" @click="remove(row)">{{ t("images.remove") }}</el-button></template></el-table-column>
            <template #empty><el-empty :description="t('images.noImages')"/></template>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="t('images.favorites')">
          <el-table :data="favoriteImages" stripe row-key="id">
            <el-table-column prop="name" :label="t('images.name')" min-width="180"/>
            <el-table-column prop="incus_ref" :label="t('images.incusRef')" min-width="220"/>
            <el-table-column prop="cuda_major" label="CUDA" width="80"/>
            <el-table-column :label="t('images.actions')" width="170" fixed="right">
              <template #default="{row}">
                <el-button size="small" :icon="StarFilled" @click="toggleFavorite(row)">{{ t("images.unfavorite") }}</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty :description="t('images.noFavorites')"/></template>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="t('images.storedImages')">
          <div class="card-header">
            <el-switch v-model="onlyMyStoredImages" :active-text="t('images.onlyMine')" :inactive-text="t('images.showAll')" />
          </div>
          <el-table :data="storedImagesForDisplay" stripe><el-table-column prop="alias" label="Alias"/><el-table-column prop="owner" :label="t('images.owner')" width="120"/><el-table-column prop="source_node" :label="t('images.sourceNode')" width="160"/><el-table-column prop="architecture" :label="t('images.architecture')" width="110"/><el-table-column prop="status" :label="t('images.status')" width="110"/><el-table-column :label="t('images.size')" width="120"><template #default="{row}">{{ Math.round(row.size_bytes/1024/1024) }} MB</template></el-table-column><el-table-column :label="t('images.actions')" width="160" fixed="right"><template #default="{row}"><el-button size="small" type="primary" :icon="Plus" @click="registerStored(row)">{{ t("images.register") }}</el-button><el-button size="small" type="danger" :icon="Delete" @click="removeStoredImage(row)">{{ t("images.remove") }}</el-button></template></el-table-column></el-table>
        </el-tab-pane>        <el-tab-pane :label="t('images.nodeLocalImages')">
          <div class="card-header">
            <span style="font-size:13px;color:var(--el-text-color-secondary)">{{ t("images.nodeLocalImagesDesc") }}</span>
            <el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
          </div>
          <el-table :data="nodeImageGroups" stripe>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div v-if="computeNodes.length === 0" style="padding:12px 24px;color:#999">{{ t("images.noOnlineComputeNodes") }}</div>
                <div v-else style="padding:8px 24px;display:flex;flex-wrap:wrap;gap:8px">
                  <div v-for="n in computeNodes" :key="n.id" style="display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid #e4e7ed;border-radius:4px;background:#fafafa">
                    <span style="font-size:13px">{{ n.hostname }}</span>
                    <el-tag v-if="row.nodes.some((rn: any) => rn.id === n.id)" type="success" size="small">{{ t("images.synced") }}</el-tag>
                    <el-button v-else size="small" type="primary" plain :icon="Upload" @click="distributeNodeImageToNode(row, n.id)">{{ t("images.sync") }}</el-button>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('images.alias')" min-width="200">
              <template #default="{ row }">
                <span style="font-family:monospace">{{ row.primaryAlias }}</span>
                <el-tag v-if="isNodeImageRegistered(row)" type="success" size="small" style="margin-left:6px">{{ t("images.alreadyRegistered") }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="description" :label="t('images.description')" min-width="180" show-overflow-tooltip />
            <el-table-column prop="architecture" :label="t('images.architecture')" width="90" />
            <el-table-column :label="t('images.availableNodes')" min-width="260">
              <template #default="{ row }">
                <div v-for="n in row.nodes" :key="n.id" style="display:inline-flex;align-items:center;gap:2px;margin:2px">
                  <el-tag size="small">{{ n.hostname }}</el-tag>
                  <el-button
                    size="small" type="danger" :icon="Delete" link
                    :title="t('images.removeFromNode')"
                    @click.stop="removeFromNode(row, n)"
                  />
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('images.fingerprint')" width="130">
              <template #default="{ row }">
                <span style="font-size:12px;font-family:monospace;color:var(--el-text-color-secondary)">{{ row.fingerprint.slice(0, 12) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('images.actions')" width="100" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" :icon="Plus" @click="registerFromNode(row)">{{ t("images.register") }}</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty :description="t('images.noNodeImages')" /></template>
          </el-table>
        </el-tab-pane>        <el-tab-pane :label="t('images.remoteUbuntu')">
          <div class="card-header">
            <div style="display:flex;gap:8px;align-items:center">
              <el-select v-model="remoteArch" style="width:110px" :placeholder="t('images.architecture')">
                <el-option :label="t('images.allArchitectures')" value=""/>
                <el-option v-for="a in remoteArchOptions" :key="a" :label="a" :value="a"/>
              </el-select>
              <el-select v-model="remoteVariant" style="width:120px" :placeholder="t('images.variant')">
                <el-option :label="t('images.allVariants')" value=""/>
                <el-option v-for="v in remoteVariantOptions" :key="v" :label="v" :value="v"/>
              </el-select>
              <el-tag v-if="remoteLoaded" type="info" style="margin-left:4px">{{ t("images.totalCount", { count: displayedRemoteImages.length }) }}</el-tag>
            </div>
            <el-button :icon="Refresh" :loading="remoteLoading" @click="loadRemote">{{ t("common.refresh") }}</el-button>
          </div>
          <el-table :data="displayedRemoteImages" v-loading="remoteLoading" stripe>
            <el-table-column :label="t('images.version')" width="90">
              <template #default="{row}">
                <strong v-if="row.version">{{ row.version }}</strong>
                <span v-else class="text-muted">{{ row.release }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="release" :label="t('images.release')" width="100"/>
            <el-table-column prop="arch" :label="t('images.architecture')" width="90"/>
            <el-table-column prop="variant" :label="t('images.variant')" width="100"/>
            <el-table-column prop="incus_ref" :label="t('images.incusRef')" min-width="220"/>
            <el-table-column prop="latest_serial" :label="t('images.latestVersion')" width="160"/>
            <el-table-column :label="t('images.actions')" width="100" fixed="right">
              <template #default="{row}">
                <el-button size="small" type="primary" :icon="Plus" @click="registerRemote(row)">{{ t("images.register") }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>
    <el-dialog v-model="dialog" :title="editingId?t('images.editPlatformImage'):t('images.registerPlatformImage')" width="680px"><el-form :model="form" label-position="top" class="form-grid"><el-form-item :label="t('images.imageId')"><el-input v-model="form.id" :disabled="Boolean(editingId)"/></el-form-item><el-form-item :label="t('images.displayName')"><el-input v-model="form.name"/></el-form-item><el-form-item label="Incus alias / fingerprint"><el-input v-model="form.incus_ref"/></el-form-item><el-form-item :label="t('images.cudaMajor')"><el-input-number v-model="form.cuda_major" :min="0"/></el-form-item><el-form-item :label="t('images.visibleInCreate')"><el-switch v-model="form.enabled"/></el-form-item><el-form-item :label="t('images.preferred')"><el-switch v-model="form.preferred"/></el-form-item></el-form><template #footer><el-button :icon="Close" @click="dialog=false">{{ t("common.cancel") }}</el-button><el-button type="primary" :icon="Select" @click="submit">{{ t("common.save") }}</el-button></template></el-dialog>
  </div>
</template>
