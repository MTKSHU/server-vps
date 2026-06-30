<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CopyDocument, Delete, Document, Plus, Refresh } from "@element-plus/icons-vue";
import StatusTag from "../components/StatusTag.vue";
import { createJoinToken, deleteJoinToken, getJoinTokens, getNodes, type JoinToken, type JoinTokenResult, type Node } from "../api/cluster";

const loading = ref(false);
const tokens = ref<JoinToken[]>([]);
const nodes = ref<Node[]>([]);
const result = ref<JoinTokenResult | null>(null);
const form = reactive({
  expected_hostname: "",
  server_url: window.location.origin,
  expires_in_hours: 24,
  note: ""
});

const detailToken = ref<JoinToken | null>(null);
const detailVisible = ref(false);
const guideOpen = ref<string[]>([]);

const agentDownloadUrl = computed(() => {
  const base = (form.server_url || window.location.origin).replace(/\/+$/, "");
  return `${base}/api/agent-releases/latest/download?architecture=amd64`;
});

const envFileTemplate = computed(() => {
  const base = (form.server_url || window.location.origin).replace(/\/+$/, "");
  const hostname = form.expected_hostname ? `\nCLUSTER_HOSTNAME=${form.expected_hostname}` : "";
  return `CLUSTER_SERVER_URL=${base}\nCLUSTER_NODE_TOKEN=<在下方生成 token 后替换>${hostname}\nCLUSTER_DATA_PATH=/data\nCLUSTER_INCUS_STORAGE_POOL=data`;
});

const systemdServiceContent = `[Unit]\nDescription=GPU cluster node agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nEnvironmentFile=/etc/cluster-node-agent.env\nExecStart=/usr/local/bin/cluster-node-agent --server \${CLUSTER_SERVER_URL} --token \${CLUSTER_NODE_TOKEN} --data-path \${CLUSTER_DATA_PATH} --incus-storage-pool \${CLUSTER_INCUS_STORAGE_POOL} --interval 60\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target`;

const agentUpdaterDownloadUrl = computed(() => {
  const base = (form.server_url || window.location.origin).replace(/\/+$/, "");
  return `${base}/api/agent-releases/latest/download-updater?architecture=amd64`;
});

function formatTime(ts: number) {
  return ts ? new Date(ts * 1000).toLocaleString() : "-";
}

function nodeName(id: number | null) {
  return nodes.value.find((node) => node.id === id)?.hostname || "-";
}

async function load() {
  loading.value = true;
  try {
    [tokens.value, nodes.value] = await Promise.all([getJoinTokens(), getNodes()]);
  } finally {
    loading.value = false;
  }
}

async function submit() {
  result.value = await createJoinToken(form);
  ElMessage.success("接入 token 已生成");
  await load();
}

async function copy(text: string) {
  await navigator.clipboard.writeText(text);
  ElMessage.success("已复制");
}

function viewInfo(row: JoinToken) {
  detailToken.value = row;
  detailVisible.value = true;
}

async function removeToken(row: JoinToken) {
  try {
    await ElMessageBox.confirm(
      `确认删除接入 token（预期主机：${row.expected_hostname || "未指定"}）？`,
      "删除确认",
      { type: "warning" }
    );
  } catch {
    return;
  }
  try {
    await deleteJoinToken(row.id);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

function detailCommand(token: JoinToken) {
  const hostname = token.expected_hostname ? ` --hostname ${token.expected_hostname}` : "";
  return `cluster-node-agent --server ${token.server_url || "<server_url>"} --token <完整token> ${hostname}--data-path /data --incus-storage-pool data`.trim();
}

function detailEnvFile(token: JoinToken) {
  return [
    `CLUSTER_SERVER_URL=${token.server_url || "<server_url>"}`,
    `CLUSTER_NODE_TOKEN=<完整token>`,
    `CLUSTER_DATA_PATH=/data`,
    `CLUSTER_INCUS_STORAGE_POOL=data`,
  ].join("\n");
}

onMounted(load);
</script>

<template>
  <div class="page-stack" v-loading="loading">
    <!-- 操作手册 -->
    <el-collapse v-model="guideOpen">
      <el-collapse-item name="guide">
        <template #title>
          <span style="font-weight:600;font-size:14px">📋 节点接入操作手册</span>
        </template>
        <el-steps direction="vertical" :active="5" class="onboarding-steps">

          <el-step title="环境准备">
            <template #description>
              <p class="step-desc">确保新机器已安装并正常运行以下组件：</p>
              <ul class="step-list">
                <li>NVIDIA 驱动 + <code>nvidia-smi</code> 可用（GPU 节点）</li>
                <li>Incus 已初始化，存储池名称为 <code>data</code>（或在 agent 启动参数中指定 <code>--incus-storage-pool</code>）。节点磁盘监控会使用该存储池的 <code>incus storage info</code> 数据</li>
                <li>新机器能访问管理节点后端地址（HTTP/HTTPS 入口）</li>

              </ul>
              <div class="step-code">
                <div class="step-code-header"><span>快速验证</span></div>
                <pre>nvidia-smi&#10;incus storage list&#10;incus profile show default</pre>
              </div>
            </template>
          </el-step>

          <el-step title="下载 Agent 二进制">
            <template #description>
              <p class="step-desc">在新节点上执行，从本平台下载最新 agent：</p>
              <div class="step-code">
                <div class="step-code-header">
                  <span>在新节点上执行</span>
                  <el-button size="small" :icon="CopyDocument" @click="copy(`curl -fsSL -H 'Authorization: Bearer <token>' '${agentDownloadUrl}' -o /usr/local/bin/cluster-node-agent && chmod +x /usr/local/bin/cluster-node-agent`)">复制</el-button>
                </div>
                <pre>curl -fsSL \
  -H 'Authorization: Bearer &lt;token&gt;' \
  '{{ agentDownloadUrl }}' \
  -o /usr/local/bin/cluster-node-agent&#10;&#10;chmod +x /usr/local/bin/cluster-node-agent&#10;cluster-node-agent --version</pre>
              </div>
              <p class="step-desc" style="margin-top:8px">或者在「Agent 发布」中下载二进制后手动 scp 上传：</p>
              <div class="step-code">
                <div class="step-code-header"><span>scp 上传</span></div>
                <pre>scp cluster-node-agent root@&lt;节点IP&gt;:/usr/local/bin/&#10;ssh root@&lt;节点IP&gt; chmod +x /usr/local/bin/cluster-node-agent</pre>
              </div>
            </template>
          </el-step>

          <el-step title="创建 systemd 服务">
            <template #description>
              <p class="step-desc">先在下方「生成接入 token」区域生成 token，然后填写配置文件：</p>
              <div class="step-code">
                <div class="step-code-header">
                  <span>/etc/cluster-node-agent.env</span>
                  <el-button size="small" :icon="CopyDocument" @click="copy(envFileTemplate)">复制</el-button>
                </div>
                <pre>{{ envFileTemplate }}</pre>
              </div>
              <div class="step-code" style="margin-top:8px">
                <div class="step-code-header">
                  <span>/etc/systemd/system/cluster-node-agent.service</span>
                  <el-button size="small" :icon="CopyDocument" @click="copy(systemdServiceContent)">复制</el-button>
                </div>
                <pre>{{ systemdServiceContent }}</pre>
              </div>
              <div class="step-code" style="margin-top:8px">
                <div class="step-code-header"><span>启用并启动服务</span></div>
                <pre>systemctl daemon-reload&#10;systemctl enable --now cluster-node-agent&#10;systemctl status cluster-node-agent</pre>
              </div>
            </template>
          </el-step>

          <el-step title="配置 Agent 自动更新（可选）">
            <template #description>
              <p class="step-desc">安装 <code>cluster-agent-updater</code> 以支持在平台界面一键升级 agent 版本：</p>
              <div class="step-code">
                <div class="step-code-header">
                  <span>下载 cluster-agent-updater</span>
                  <el-button size="small" :icon="CopyDocument" @click="copy(`curl -fsSL -H 'Authorization: Bearer <token>' '${agentUpdaterDownloadUrl}' -o /usr/local/bin/cluster-agent-updater && chmod +x /usr/local/bin/cluster-agent-updater`)">复制</el-button>
                </div>
                <pre>curl -fsSL \
  -H 'Authorization: Bearer &lt;token&gt;' \
  '{{ agentUpdaterDownloadUrl }}' \
  -o /usr/local/bin/cluster-agent-updater&#10;&#10;chmod +x /usr/local/bin/cluster-agent-updater</pre>
              </div>
              <div class="step-code" style="margin-top:8px">
                <div class="step-code-header"><span>/etc/systemd/system/cluster-agent-updater.service</span></div>
                <pre>[Unit]&#10;Description=Check for GPU cluster node agent updates&#10;&#10;[Service]&#10;Type=oneshot&#10;EnvironmentFile=/etc/cluster-node-agent.env&#10;ExecStart=/usr/local/bin/cluster-agent-updater \\ &#10;  --server ${CLUSTER_SERVER_URL} \\ &#10;  --token ${CLUSTER_NODE_TOKEN}&#10;&#10;[Install]&#10;WantedBy=multi-user.target</pre>
              </div>
              <div class="step-code" style="margin-top:8px">
                <div class="step-code-header"><span>/etc/systemd/system/cluster-agent-updater.timer</span></div>
                <pre>[Unit]&#10;Description=Check for GPU cluster node agent updates&#10;&#10;[Timer]&#10;OnBootSec=5min&#10;OnUnitActiveSec=15min&#10;RandomizedDelaySec=5min&#10;Persistent=true&#10;&#10;[Install]&#10;WantedBy=timers.target</pre>
              </div>
              <div class="step-code" style="margin-top:8px">
                <div class="step-code-header"><span>启用 timer</span></div>
                <pre>systemctl daemon-reload&#10;systemctl enable --now cluster-agent-updater.timer</pre>
              </div>
            </template>
          </el-step>

          <el-step title="验证上线">
            <template #description>
              <p class="step-desc">agent 启动后约 10 秒即可在节点列表中看到新节点（状态 <code>online</code>）。如未出现，检查 agent 日志：</p>
              <div class="step-code">
                <div class="step-code-header"><span>查看 agent 日志</span></div>
                <pre>journalctl -u cluster-node-agent -f</pre>
              </div>
              <ul class="step-list" style="margin-top:8px">
                <li>常见问题：token 过期或已使用 → 在下方重新生成一个</li>
                <li>常见问题：网络不通 → 确认管理节点 URL 可从新机器访问</li>
                <li>常见问题：Incus 未就绪 → agent 上报 <code>incus_status=unknown</code>，在节点配置中手动修正</li>
              </ul>
            </template>
          </el-step>

        </el-steps>
      </el-collapse-item>
    </el-collapse>
    <el-card shadow="never">
      <template #header><strong>生成新节点接入 token</strong></template>
      <el-form :model="form" label-position="top" class="form-grid">
        <el-form-item label="预期主机名">
          <el-input v-model="form.expected_hostname" placeholder="gpu-4090-01" />
        </el-form-item>
        <el-form-item label="有效期小时">
          <el-input-number v-model="form.expires_in_hours" :min="1" :max="720" />
        </el-form-item>
        <el-form-item label="管理节点后端地址" class="wide">
          <el-input v-model="form.server_url" />
        </el-form-item>
        <el-form-item label="备注" class="wide">
          <el-input v-model="form.note" placeholder="机房、负责人或批次说明" />
        </el-form-item>
      </el-form>
      <el-button type="primary" :icon="Plus" @click="submit">生成接入 token</el-button>
    </el-card>

    <el-card v-if="result" shadow="never">
      <template #header>
        <div class="card-header">
          <strong>一次性接入信息</strong>
          <el-tag type="warning">完整 token 只显示一次</el-tag>
        </div>
      </template>
      <div class="code-section">
        <div class="card-header">
          <span>启动命令</span>
          <el-button :icon="CopyDocument" @click="copy(result.command)">复制</el-button>
        </div>
        <pre>{{ result.command }}</pre>
      </div>
      <div class="code-section">
        <div class="card-header">
          <span>/etc/cluster-node-agent.env</span>
          <el-button :icon="CopyDocument" @click="copy(result.env_file)">复制</el-button>
        </div>
        <pre>{{ result.env_file }}</pre>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <strong>接入 token</strong>
          <el-button :icon="Refresh" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table :data="tokens" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column label="尾号" width="120">
          <template #default="{ row }"><code>...{{ row.token_preview }}</code></template>
        </el-table-column>
        <el-table-column prop="expected_hostname" label="预期主机" min-width="160" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }"><StatusTag :value="row.status" /></template>
        </el-table-column>
        <el-table-column label="绑定节点" min-width="150">
          <template #default="{ row }">{{ nodeName(row.node_id) }}</template>
        </el-table-column>
        <el-table-column label="过期时间" min-width="180">
          <template #default="{ row }">{{ formatTime(row.expires_at) }}</template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="160" />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :icon="Document" @click="viewInfo(row)">配置信息</el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="removeToken(row)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>

  <!-- 配置信息 dialog -->
  <el-dialog v-model="detailVisible" title="接入配置信息" width="600px">
    <template v-if="detailToken">
      <el-descriptions :column="2" border size="small" style="margin-bottom:16px">
        <el-descriptions-item label="ID">{{ detailToken.id }}</el-descriptions-item>
        <el-descriptions-item label="token 尾号"><code>...{{ detailToken.token_preview }}</code></el-descriptions-item>
        <el-descriptions-item label="预期主机">{{ detailToken.expected_hostname || "未指定" }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :value="detailToken.status" /></el-descriptions-item>
        <el-descriptions-item label="绑定节点">{{ nodeName(detailToken.node_id) }}</el-descriptions-item>
        <el-descriptions-item label="过期时间">{{ formatTime(detailToken.expires_at) }}</el-descriptions-item>
        <el-descriptions-item label="备注" :span="2">{{ detailToken.note || "—" }}</el-descriptions-item>
      </el-descriptions>
      <el-alert type="warning" :closable="false" style="margin-bottom:12px">
        完整 token 仅在生成时显示一次，以下命令中 &lt;完整token&gt; 需替换为实际值
      </el-alert>
      <div class="code-section">
        <div class="card-header">
          <span>启动命令参考</span>
          <el-button size="small" :icon="CopyDocument" @click="copy(detailCommand(detailToken))">复制</el-button>
        </div>
        <pre>{{ detailCommand(detailToken) }}</pre>
      </div>
      <div class="code-section">
        <div class="card-header">
          <span>/etc/cluster-node-agent.env 参考</span>
          <el-button size="small" :icon="CopyDocument" @click="copy(detailEnvFile(detailToken))">复制</el-button>
        </div>
        <pre>{{ detailEnvFile(detailToken) }}</pre>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.onboarding-steps {
  padding: 8px 0 4px;
}

.onboarding-steps :deep(.el-step__description) {
  padding-bottom: 16px;
}

.step-desc {
  margin: 4px 0 8px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.step-list {
  margin: 0 0 8px 20px;
  padding: 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.8;
}

.step-code {
  background: #1e1e2e;
  border-radius: 6px;
  overflow: hidden;
  font-size: 12px;
}

.step-code-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 12px;
  background: #16161e;
  color: #7f849c;
  font-size: 12px;
}

.step-code pre {
  margin: 0;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
  color: #cdd6f4;
}
</style>
