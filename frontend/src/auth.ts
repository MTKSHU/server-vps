import { ref } from "vue";

export interface AuthUser {
  id: number;
  username: string;
  display_name: string;
  role: "admin" | "member";
  group_name: "platform_admin" | "admin" | "member" | "guest";
}

const tokenKey = "cluster.auth.token";
const userKey = "cluster.auth.user";
export const authToken = ref(localStorage.getItem(tokenKey) || "");
export const authUser = ref<AuthUser | null>(JSON.parse(localStorage.getItem(userKey) || "null"));

export function setAuth(token: string, user: AuthUser) {
  authToken.value = token;
  authUser.value = user;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem(userKey, JSON.stringify(user));
}

export function hasAdminAccess(user: AuthUser | null = authUser.value) {
  return user?.role === "admin" || user?.group_name === "platform_admin" || user?.group_name === "admin";
}

export function clearAuth() {
  authToken.value = "";
  authUser.value = null;
  localStorage.removeItem(tokenKey);
  localStorage.removeItem(userKey);
}
