<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Cpu,
  Gauge,
  LayoutDashboard,
  Layers,
  HardDrive,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  Server,
  User,
  Users
} from "lucide-vue-next";
import { authUser, clearAuth, hasAdminAccess } from "../auth";
import { logout } from "../api/cluster";

const route = useRoute();
const router = useRouter();
const isSidebarCollapsed = ref(false);
const active = computed(() => {
  const name = route.name?.toString() || "dashboard";
  if (name === "nodeJoin") return "nodes";
  if (name === "create") return "containers";
  return name;
});
const sidebarWidth = computed(() => (isSidebarCollapsed.value ? "72px" : "248px"));
const isAdmin = computed(() => hasAdminAccess());

const items = computed(() => [
  { name: "dashboard", label: "仪表盘", icon: LayoutDashboard },
  ...(isAdmin.value ? [{ name: "nodes", label: "节点管理", icon: Server }] : []),
  { name: "containers", label: "容器管理", icon: Cpu },
  ...(isAdmin.value ? [{ name: "images", label: "镜像管理", icon: Layers }] : []),
  { name: "storage", label: "存储中心", icon: HardDrive },
  { name: "profile", label: "个人信息", icon: User },
  ...(isAdmin.value ? [{ name: "settings", label: "平台设置", icon: Settings }] : []),
  ...(isAdmin.value ? [{ name: "users", label: "用户管理", icon: Users }] : [])
]);

async function signOut() {
  try { await logout(); } finally { clearAuth(); router.replace({ name: "login" }); }
}

function navigate(name: string) {
  router.push({ name });
}
</script>

<template>
  <el-container class="layout">
    <el-aside :width="sidebarWidth" class="sidebar" :class="{ 'is-collapsed': isSidebarCollapsed }">
      <div class="brand">
        <div class="brand-mark">
          <Gauge :size="22" />
        </div>
        <div class="brand-copy">
          <strong>GPU 集群平台</strong>
          <span>Incus VPS Console</span>
        </div>
        <el-tooltip :content="isSidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'" placement="right">
          <button
            class="sidebar-toggle"
            type="button"
            :aria-label="isSidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
            @click="isSidebarCollapsed = !isSidebarCollapsed"
          >
            <PanelLeftOpen v-if="isSidebarCollapsed" :size="18" />
            <PanelLeftClose v-else :size="18" />
          </button>
        </el-tooltip>
      </div>

      <el-menu
        :default-active="active"
        :collapse="isSidebarCollapsed"
        :collapse-transition="false"
        class="menu"
        @select="navigate"
      >
        <el-menu-item v-for="item in items" :key="item.name" :index="item.name">
          <component :is="item.icon" :size="18" />
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div>
          <h1>{{ route.meta.title }}</h1>
          <p>GPU 容器、公开存储与计算资源管理</p>
        </div>
        <div class="header-actions">
          <el-tag type="info">{{ authUser?.display_name || authUser?.username }}</el-tag>
          <el-button :icon="LogOut" @click="signOut">退出</el-button>
        </div>
      </el-header>
      <el-main class="main">
        <RouterView />
      </el-main>
    </el-container>
  </el-container>
</template>
