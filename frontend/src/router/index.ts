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
    { path: "/login", name: "login", component: Login, meta: { public: true, title: "登录" } },
    { path: "/platform-login", name: "platformLogin", component: Login, meta: { public: true, title: "平台登录" } },
    { path: "/register", name: "register", component: Register, meta: { public: true, title: "注册" } },
    { path: "/login/callback", name: "loginCallback", component: LoginCallback, meta: { public: true, title: "认证中" } },
    {
      path: "/",
      component: AdminLayout,
      children: [
        { path: "", name: "dashboard", component: Dashboard, meta: { title: "仪表盘" } },
        { path: "nodes", name: "nodes", component: Nodes, meta: { title: "节点管理", admin: true } },
        { path: "node-join", name: "nodeJoin", redirect: { name: "nodes" } },
        { path: "containers", name: "containers", component: Containers, meta: { title: "容器管理" } },
        { path: "containers/:id/shell", name: "containerShell", component: ContainerShell, meta: { title: "容器 Shell" } },
        { path: "nodes/:id/shell", name: "nodeShell", component: NodeShell, meta: { title: "节点 Shell", admin: true } },
        { path: "create", name: "create", redirect: { name: "containers" } },
        { path: "images", name: "images", component: Images, meta: { title: "镜像管理", admin: true } },
        { path: "storage", name: "storage", component: StorageCenter, meta: { title: "存储中心" } },
        { path: "profile", name: "profile", component: Profile, meta: { title: "个人信息" } },
        { path: "settings", name: "settings", component: PlatformSettings, meta: { title: "平台设置", admin: true } },
        { path: "users", name: "users", component: Users, meta: { title: "用户管理", admin: true } }
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
