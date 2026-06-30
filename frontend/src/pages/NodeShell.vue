<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Back, FullScreen } from "@element-plus/icons-vue";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { getNodes, type Node } from "../api/cluster";
import { authToken } from "../auth";

const route = useRoute();
const router = useRouter();
const nodeId = Number(route.params.id);
const node = ref<Node | null>(null);
const terminalHost = ref<HTMLDivElement | null>(null);
const maximized = ref(false);

let terminal: Terminal | null = null;
let fitAddon: FitAddon | null = null;
let socket: WebSocket | null = null;
let resizeObserver: ResizeObserver | null = null;

function terminalUrl(cols: number, rows: number) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({ cols: String(cols), rows: String(rows), token: authToken.value });
  return `${protocol}//${window.location.host}/api/nodes/${nodeId}/terminal?${params.toString()}`;
}

function fitAndNotify() {
  if (!terminal || !fitAddon) return;
  fitAddon.fit();
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
  }
}

function toggleMaximize() {
  maximized.value = !maximized.value;
  nextTick(fitAndNotify);
}

async function loadNode() {
  const nodes = await getNodes();
  node.value = nodes.find((n) => n.id === nodeId) || null;
  if (!node.value) {
    ElMessage.error("节点不存在");
    router.push({ name: "nodes" });
    return false;
  }
  return true;
}

async function openTerminal() {
  if (!terminalHost.value) return;
  terminal = new Terminal({
    cursorBlink: true,
    convertEol: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 14,
    theme: {
      background: "#10181d",
      foreground: "#e6f2ef",
      cursor: "#2aa89d",
      selectionBackground: "#31535d",
    },
  });
  fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(terminalHost.value);
  fitAddon.fit();
  terminal.writeln(`connecting to ${node.value?.hostname}...`);

  socket = new WebSocket(terminalUrl(terminal.cols, terminal.rows));
  socket.addEventListener("open", () => {
    terminal?.clear();
    fitAndNotify();
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "data") terminal?.write(message.data);
    if (message.type === "started") terminal?.focus();
    if (message.type === "error") terminal?.writeln(`\r\n\x1b[31m${message.error}\x1b[0m`);
    if (message.type === "exit") terminal?.writeln("\r\n\x1b[90m[session closed]\x1b[0m");
  });
  socket.addEventListener("close", () => {
    terminal?.writeln("\r\n\x1b[90m[connection closed]\x1b[0m");
  });
  socket.addEventListener("error", () => {
    terminal?.writeln("\r\n\x1b[31m[terminal websocket error]\x1b[0m");
  });
  terminal.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "input", data }));
    }
  });
  terminal.onResize(({ cols, rows }) => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });
  resizeObserver = new ResizeObserver(fitAndNotify);
  resizeObserver.observe(terminalHost.value);
  window.addEventListener("resize", fitAndNotify);
}

onMounted(async () => {
  if (await loadNode()) {
    await nextTick();
    await openTerminal();
  }
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  window.removeEventListener("resize", fitAndNotify);
  socket?.close();
  terminal?.dispose();
});
</script>

<template>
  <div :class="['page-stack', { 'shell-fullscreen': maximized }]">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <strong>{{ node?.hostname || "Shell" }}</strong>
          <div style="display:flex;gap:8px">
            <el-button :icon="FullScreen" @click="toggleMaximize">{{ maximized ? '恢复窗口' : '最大化' }}</el-button>
            <el-button :icon="Back" @click="router.push({ name: 'nodes' })">返回</el-button>
          </div>
        </div>
      </template>
      <div ref="terminalHost" class="terminal-host" />
    </el-card>
  </div>
</template>

<style scoped>
.shell-fullscreen {
  position: fixed !important;
  inset: 0;
  z-index: 2000;
  padding: 0;
  overflow: hidden;
  background: #10181d;
}
.shell-fullscreen :deep(.el-card) {
  height: 100vh;
  border-radius: 0;
  display: flex;
  flex-direction: column;
}
.shell-fullscreen :deep(.el-card__body) {
  flex: 1;
  padding: 0;
  overflow: hidden;
}
.shell-fullscreen :deep(.terminal-host) {
  height: 100%;
  min-height: unset;
  border-radius: 0;
}
</style>
