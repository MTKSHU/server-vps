import { createRouter, createWebHistory } from "vue-router";
import AdminLayout from "../layouts/AdminLayout.vue";
import Dashboard from "../pages/Dashboard.vue";
import Nodes from "../pages/Nodes.vue";
import Containers from "../pages/Containers.vue";
import ContainerShell from "../pages/ContainerShell.vue";
import NodeShell from "../pages/NodeShell.vue";
import Images from "../pages/Images.vue";
import Users from "../pages/Users.vue";
import Profile from "../pages/Profile.vue";
import PlatformSettings from "../pages/PlatformSettings.vue";
import StorageCenter from "../pages/StorageCenter.vue";
import Login from "../pages/Login.vue";
import LoginCallback from "../pages/LoginCallback.vue";
import Register from "../pages/Register.vue";
import { authToken, authUser, hasAdminAccess } from "../auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: Login, meta: { public: true, titleKey: "common.login" } },
    { path: "/platform-login", name: "platformLogin", component: Login, meta: { public: true, titleKey: "auth.platformLogin" } },
    { path: "/register", name: "register", component: Register, meta: { public: true, titleKey: "common.register" } },
    { path: "/login/callback", name: "loginCallback", component: LoginCallback, meta: { public: true, titleKey: "auth.authenticating" } },
    {
      path: "/",
      component: AdminLayout,
      children: [
        { path: "", name: "dashboard", component: Dashboard, meta: { titleKey: "nav.dashboard" } },
        { path: "nodes", name: "nodes", component: Nodes, meta: { titleKey: "nav.nodes", admin: true } },
        { path: "node-join", name: "nodeJoin", redirect: { name: "nodes" } },
        { path: "containers", name: "containers", component: Containers, meta: { titleKey: "nav.containers" } },
        { path: "containers/:id/shell", name: "containerShell", component: ContainerShell, meta: { titleKey: "nav.containerShell" } },
        { path: "nodes/:id/shell", name: "nodeShell", component: NodeShell, meta: { titleKey: "nav.nodeShell", admin: true } },
        { path: "create", name: "create", redirect: { name: "containers" } },
        { path: "images", name: "images", component: Images, meta: { titleKey: "nav.images", admin: true } },
        { path: "storage", name: "storage", component: StorageCenter, meta: { titleKey: "nav.storage" } },
        { path: "profile", name: "profile", component: Profile, meta: { titleKey: "nav.profile" } },
        { path: "settings", name: "settings", component: PlatformSettings, meta: { titleKey: "nav.settings", admin: true } },
        { path: "users", name: "users", component: Users, meta: { titleKey: "nav.users", admin: true } }
      ]
    }
  ]
});

router.beforeEach((to) => {
  if (!to.meta.public && !authToken.value) return { name: "login", query: { redirect: to.fullPath } };
  if ((to.name === "login" || to.name === "platformLogin" || to.name === "register") && authToken.value) return { name: "dashboard" };
  if (to.meta.admin && !hasAdminAccess(authUser.value)) return { name: "dashboard" };
});

export default router;
