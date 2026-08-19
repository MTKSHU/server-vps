<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
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
import LanguageSwitcher from "../components/LanguageSwitcher.vue";

const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const isSidebarCollapsed = ref(false);
const active = computed(() => {
  const name = route.name?.toString() || "dashboard";
  if (name === "nodeJoin") return "nodes";
  if (name === "create") return "containers";
  return name;
});
const sidebarWidth = computed(() => (isSidebarCollapsed.value ? "72px" : "248px"));
const isAdmin = computed(() => hasAdminAccess());
const sidebarToggleLabel = computed(() => isSidebarCollapsed.value ? t("layout.expandSidebar") : t("layout.collapseSidebar"));
const routeTitle = computed(() => {
  const key = route.meta.titleKey;
  return typeof key === "string" ? t(key) : String(route.meta.title || "");
});

const items = computed(() => [
  { name: "dashboard", label: t("nav.dashboard"), icon: LayoutDashboard },
  ...(isAdmin.value ? [{ name: "nodes", label: t("nav.nodes"), icon: Server }] : []),
  { name: "containers", label: t("nav.containers"), icon: Cpu },
  { name: "images", label: t("nav.images"), icon: Layers },
  { name: "storage", label: t("nav.storage"), icon: HardDrive },
  { name: "profile", label: t("nav.profile"), icon: User },
  ...(isAdmin.value ? [{ name: "settings", label: t("nav.settings"), icon: Settings }] : []),
  ...(isAdmin.value ? [{ name: "users", label: t("nav.users"), icon: Users }] : [])
]);

async function signOut() {
  try { await logout(); } finally { clearAuth(); router.replace({ name: "login" }); }
}

function navigate(name: string) {
  router.push({ name });
}

function handleUserCommand(command: string) {
  if (command === "profile") router.push({ name: "profile" });
  if (command === "logout") signOut();
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
          <strong>{{ t("app.name") }}</strong>
          <span>{{ t("app.subtitle") }}</span>
        </div>
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

      <div class="sidebar-footer">
        <el-tooltip :content="sidebarToggleLabel" placement="right">
          <button
            class="sidebar-toggle"
            type="button"
            :aria-label="sidebarToggleLabel"
            @click="isSidebarCollapsed = !isSidebarCollapsed"
          >
            <PanelLeftOpen v-if="isSidebarCollapsed" :size="18" />
            <PanelLeftClose v-else :size="18" />
          </button>
        </el-tooltip>
      </div>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div>
          <h1>{{ routeTitle }}</h1>
          <p>{{ t("app.tagline") }}</p>
        </div>
        <div class="header-actions">
          <LanguageSwitcher />
          <el-dropdown trigger="click" @command="handleUserCommand">
            <el-button class="user-menu-button" data-keep-label="true" :aria-label="t('layout.userMenu')">
              <User :size="16" />
              <span>{{ authUser?.display_name || authUser?.username }}</span>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <User :size="15" />
                  <span>{{ t("nav.profile") }}</span>
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <LogOut :size="15" />
                  <span>{{ t("common.logout") }}</span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="main">
        <RouterView />
      </el-main>
    </el-container>
  </el-container>
</template>
