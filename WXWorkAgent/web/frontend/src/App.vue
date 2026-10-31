<template>
  <div class="app-shell">
    <!-- 左侧导航栏 -->
    <nav class="sidebar">
      <div class="sidebar-logo">
        <span class="logo-icon" aria-hidden="true"><Bot :size="22" :stroke-width="2" /></span>
        <span class="logo-text">Agent Console</span>
      </div>
      <div class="sidebar-menu">
        <button
          v-for="tab in TABS"
          :key="tab.id"
          class="menu-item"
          :class="{ active: activeTab === tab.id }"
          @click="activeTab = tab.id"
        >
          <span class="menu-icon" aria-hidden="true"><component :is="tab.Icon" :size="18" :stroke-width="2" /></span>
          <span class="menu-label">{{ tab.label }}</span>
          <span v-if="tab.badge()" class="menu-badge">{{ tab.badge() }}</span>
        </button>
      </div>
    </nav>

    <!-- 右侧内容区 -->
    <div class="content-area">
      <!-- 顶部标题栏 -->
      <header class="content-header">
        <div class="header-left">
          <h1 class="page-title">{{ currentTab?.label }}</h1>
          <span class="page-sub">{{ currentTab?.sub }}</span>
        </div>
        <div class="header-right">
          <button class="icon-btn" :class="{ spinning: loading }" @click="refreshAll" title="刷新数据">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor">
              <path d="M14.5 3A8 8 0 1 0 15 8h-1.5a6.5 6.5 0 1 1-.52-2.57L11 7h4V3l-1.5 1.5-.5-.5z"/>
            </svg>
          </button>
        </div>
      </header>

      <!-- 客户线索 -->
      <main v-if="activeTab === 'leads'" class="page-body">
        <div class="toolbar">
          <div class="toolbar-left">
            <div class="stat-pills">
              <span class="stat-pill" v-for="s in leadStats" :key="s.label" :class="s.cls">
                {{ s.label }} <b>{{ s.val }}</b>
              </span>
            </div>
          </div>
          <div class="toolbar-right">
            <select v-model="leadsFilter" class="dy-select">
              <option value="">全部状态</option>
              <option value="pending">待处理</option>
              <option value="running">执行中</option>
              <option value="done">已成功</option>
              <option value="failed">失败</option>
            </select>
            <button class="dy-btn secondary" @click="openAddLeadModal">
              <span>+</span> 手动录入
            </button>
            <button class="dy-btn ghost" @click="seedDemo">演示数据</button>
          </div>
        </div>

        <div class="card">
          <div v-if="loading" class="empty-state">加载中…</div>
          <div v-else-if="filteredLeads.length === 0" class="empty-state">
            <div class="empty-icon">📋</div>
            <p>暂无线索数据</p>
          </div>
          <table v-else class="dy-table">
            <thead>
              <tr>
                <th>来源平台</th>
                <th>手机号</th>
                <th>客户昵称</th>
                <th>状态</th>
                <th>上报时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="lead in filteredLeads" :key="lead.id">
                <tr class="dy-row" :class="{ expanded: expandedLeadId === lead.id }" @click="toggleLeadExpand(lead.id)">
                  <td>
                    <span class="platform-tag" :class="lead.source">
                      {{ lead.source === 'douyin' ? '🎵 抖音' : lead.source === 'wxwork' ? '💬 企微' : '✏️ 手动' }}
                    </span>
                  </td>
                  <td class="mono">{{ maskPhone(lead.phone) }}</td>
                  <td class="muted">{{ lead.source_user || '—' }}</td>
                  <td><span class="status-tag" :class="lead.status">{{ statusLabel(lead.status) }}</span></td>
                  <td class="muted">{{ fmtTime(lead.created_at) }}</td>
                  <td @click.stop class="actions-cell">
                    <button
                      v-if="lead.status === 'pending' || lead.status === 'failed'"
                      class="dy-btn primary small"
                      @click="triggerLead(lead.id)"
                    >触发添加</button>
                    <button
                      v-if="lead.status !== 'running'"
                      class="dy-btn ghost small"
                      @click="resetLead(lead.id)"
                    >重置</button>
                  </td>
                </tr>
                <tr v-if="expandedLeadId === lead.id" class="expand-tr">
                  <td colspan="6">
                    <div class="expand-panel">
                      <p v-if="lead.source_context" class="ctx-text">💬 {{ lead.source_context }}</p>
                      <div v-if="lead.result_detail.length === 0" class="muted small">暂无步骤记录</div>
                      <div v-else class="steps">
                        <div v-for="(ev, i) in lead.result_detail" :key="i" class="step" :class="{ ok: ev.ok, fail: !ev.ok }">
                          <span class="step-dot"></span>
                          <span class="step-name">{{ ev.step }}</span>
                          <span v-if="ev.ts" class="step-time">{{ fmtTime(ev.ts) }}</span>
                          <span v-if="ev.detail && !ev.ok" class="step-err">{{ ev.detail }}</span>
                          <a v-if="ev.screenshot_path" :href="ev.screenshot_path" target="_blank" class="step-link">截图</a>
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </main>

      <!-- 抖音对话记录（上报到平台的即时会话 SQLite） -->
      <main v-else-if="activeTab === 'conversations'" class="page-body chat-layout">
        <div class="chat-sidebar">
          <div class="chat-sidebar-header">
            <select v-model="convPlatformFilter" class="dy-select">
              <option value="douyin">🎵 只看抖音</option>
              <option value="">全部平台（含 SQLite 内的企微上报）</option>
              <option value="wxwork">💬 仅企微上报</option>
            </select>
            <input v-model="threadSearch" class="dy-input" placeholder="搜索抖音会话联系人…" />
            <div class="dy-import-strip">
              <label class="dy-import-label">
                <span class="muted small">Douyin Agent 归档（recent_chats.json）</span>
                <select v-model="selectedDyArchive" class="dy-select" :disabled="dyImportLoading || !dyArchives.length">
                  <option value="">使用最新更新时间</option>
                  <option v-for="a in dyArchives" :key="a.folder" :value="a.folder">
                    {{ a.folder }} · {{ fmtTime(a.updated_at) }}
                  </option>
                </select>
              </label>
              <div class="dy-import-actions">
                <button class="dy-btn ghost small" type="button" :disabled="dyImportLoading" @click="loadDouyinArchives">
                  ↻ 刷新归档
                </button>
                <button
                  class="dy-btn primary small"
                  type="button"
                  :disabled="dyImportLoading || !dyArchives.length"
                  @click="importDouyinToSqlite"
                >
                  {{ dyImportLoading ? "导入中…" : "导入到 SQLite" }}
                </button>
              </div>
              <p v-if="dyImportFeedback" class="dy-import-feedback" :class="{ err: dyImportFeedbackErr }">{{ dyImportFeedback }}</p>
            </div>
          </div>
          <div class="chat-thread-list">
            <div v-if="loading" class="empty-state small">加载中…</div>
            <div v-else-if="filteredThreads.length === 0" class="empty-state small">暂无符合条件的会话（可切换左上角「只看抖音」等筛选）</div>
            <button
              v-for="t in filteredThreads"
              :key="t.peer_key"
              class="thread-item"
              :class="{ active: selectedPeerKey === t.peer_key }"
              @click="selectThread(t)"
            >
              <div class="avatar" :class="t.platform">{{ t.platform === 'douyin' ? '抖' : '企' }}</div>
              <div class="thread-info">
                <div class="thread-row">
                  <span class="thread-name">{{ t.contact_name || t.peer_key }}</span>
                  <span class="thread-time">{{ fmtTime(t.last_at) }}</span>
                </div>
                <p class="thread-preview">{{ t.last_message }}</p>
              </div>
            </button>
          </div>
        </div>

        <div class="chat-main">
          <template v-if="selectedThread">
            <div class="chat-header">
              <div class="avatar lg" :class="selectedThread.platform">{{ selectedThread.platform === 'douyin' ? '抖' : '企' }}</div>
              <div>
                <h3>{{ selectedThread.contact_name || selectedThread.peer_key }}</h3>
                <span class="muted small">{{ selectedThread.platform === 'douyin' ? '抖音' : '企业微信' }} · {{ selectedThread.message_count }} 条消息</span>
              </div>
              <div v-if="selectedThread.platform === 'douyin'" class="chat-header-actions">
                <button class="dy-btn ghost small" type="button" :disabled="revaiClearLoading" @click="clearRevaiSession">
                  {{ revaiClearLoading ? "清理中..." : "清理 RevAI Session" }}
                </button>
                <span v-if="revaiClearFeedback" class="dy-import-feedback" :class="{ err: revaiClearFeedbackErr }">{{ revaiClearFeedback }}</span>
              </div>
            </div>
            <div class="message-stream" ref="streamEl">
              <div v-for="msg in currentMessages" :key="msg.id" class="msg-row" :class="msg.direction">
                <div class="bubble">
                  <p>{{ msg.content }}</p>
                  <span class="msg-time">{{ fmtTime(msg.created_at) }}</span>
                </div>
              </div>
            </div>
          </template>
          <div v-else class="empty-state chat-ph">
            <div class="empty-icon">💬</div>
            <p>从左侧选择一个抖音会话</p>
          </div>
        </div>
      </main>

      <!-- 企微对话记录（桌面端归档的 recent_chats 数据） -->
      <main v-else-if="activeTab === 'wx_export'" class="page-body chat-layout wx-export-layout">
        <div class="wx-export-toolbar card flat">
          <div class="wx-toolbar-row">
            <label class="inline-field">
              <span class="muted small">选择会话批次（按数据更新时间排序，最新的在前）</span>
              <select
                v-model="selectedExportName"
                class="dy-select wx-export-select"
                :disabled="wxExportLoading || !wxExports.length"
                @change="onWxBatchChanged"
              >
                <option v-for="ex in wxExports" :key="ex.name" :value="ex.name">
                  {{ ex.name }} · {{ ex.conversation_count }} 个会话 · {{ ex.updated_at || ex.exported_at || "—" }}
                </option>
              </select>
            </label>
            <button class="dy-btn ghost small" type="button" :disabled="wxExportLoading" @click="loadWxExportsOnly">
              ↻ 刷新对话数据
            </button>
          </div>
          <p v-if="wxChatsData" class="wx-meta muted small">
            归档记录时间：<strong>{{ wxChatsData.exported_at || "—" }}</strong>
            · 本地数据更新时间：<strong>{{ wxChatsData.updated_at }}</strong>
            · 本轮共 <strong>{{ wxChatsData.conversation_count }}</strong> 个会话
          </p>
          <p v-if="wxExportError" class="wx-meta err small">{{ wxExportError }}</p>
        </div>

        <div class="wx-chat-split">
          <div class="chat-sidebar">
            <div class="chat-sidebar-header">
              <input v-model="wxConvSearch" class="dy-input" placeholder="搜索会话标题…" :disabled="!wxChatsData" />
            </div>
            <div class="chat-thread-list">
              <div v-if="wxExportLoading || wxChatsLoading" class="empty-state small">加载中…</div>
              <div v-else-if="!wxExports.length" class="empty-state small">
                <p>暂无企微对话存档。</p>
                <p class="muted small" style="margin-top: 8px">桌面端抓取的会话会落在项目 <code class="mono">exports/**/recent_chats.json</code>，服务端会自动发现并展示。</p>
              </div>
              <div v-else-if="!filteredWxConversations.length" class="empty-state small">无匹配的会话</div>
              <button
                v-for="c in filteredWxConversations"
                :key="c.index + '-' + c.title"
                class="thread-item"
                :class="{ active: selectedWxConv?.index === c.index && selectedWxConv?.title === c.title }"
                type="button"
                @click="selectWxConversation(c)"
              >
                <div class="avatar wx">企</div>
                <div class="thread-info">
                  <div class="thread-row">
                    <span class="thread-name">{{ c.title }}</span>
                    <span class="thread-time">{{ c.message_count }} 段</span>
                  </div>
                  <p class="thread-preview">{{ c.list_preview }}</p>
                </div>
              </button>
            </div>
          </div>

          <div class="chat-main">
            <template v-if="selectedWxConv">
              <div class="chat-header">
                <div class="avatar lg wx">企</div>
                <div>
                  <h3>{{ selectedWxConv.title }}</h3>
                  <span class="muted small">{{ selectedWxConv.message_count }} 条消息段落（含时间与气泡）</span>
                </div>
              </div>
              <div v-if="selectedWxConv.screenshot_urls?.length" class="conv-screens-row">
                <span class="muted small">会话截图：</span>
                <a
                  v-for="(su, si) in selectedWxConv.screenshot_urls"
                  :key="si"
                  class="screenshot-thumb-link"
                  :href="absoluteExportUrl(su)"
                  target="_blank"
                  rel="noopener"
                >
                  <img :src="absoluteExportUrl(su)" alt="" class="screenshot-thumb" />
                </a>
              </div>
              <div class="message-stream" ref="wxStreamEl">
                <template v-for="(m, wi) in selectedWxConv.messages" :key="wi + '-' + m.id">
                  <div v-if="m.kind === 'time'" class="msg-row wx-time-row">
                    <span class="time-chip-wx">{{ m.text }}</span>
                  </div>
                  <div v-else class="msg-row wx-msg-row" :class="wxBubbleAlign(m)">
                    <div class="bubble wx-bubble">
                      <div v-if="m.sender" class="wx-msg-sender muted small">{{ m.sender }}</div>
                      <p>{{ m.text }}</p>
                      <div v-if="m.timestamp_visible || m.timestamp" class="msg-foot muted small">
                        {{ m.timestamp_visible || m.timestamp }}
                      </div>
                      <a v-if="m.source_screenshot" class="screenshot-mini" :href="exportScreenshotUrl(m.source_screenshot)" target="_blank">查看 OCR 截图</a>
                    </div>
                  </div>
                </template>
              </div>
            </template>
            <div v-else class="empty-state chat-ph">
              <div class="empty-icon">📂</div>
              <p>选择左侧会话查看企微导出记录</p>
            </div>
          </div>
        </div>
      </main>

      <!-- 发朋友圈 -->
      <main v-else-if="activeTab === 'moments'" class="page-body">
        <div class="two-col">
          <!-- 发布表单 -->
          <div class="card form-card">
            <div class="card-title">
              <span class="card-title-icon">✍️</span> 新建朋友圈内容
            </div>
            <div class="form-body">
              <label class="form-field">
                <span class="field-label">文案内容 <em>*</em></span>
                <textarea
                  v-model="momentForm.content"
                  rows="5"
                  placeholder="输入要发布到朋友圈的内容…"
                  :disabled="momentSubmitting"
                  class="dy-textarea"
                />
              </label>
              <label class="form-field">
                <span class="field-label">配图（可选）</span>
                <div class="file-picker-row">
                  <input
                    ref="momentImageInputRef"
                    type="file"
                    accept="image/*"
                    class="file-input-sr"
                    :disabled="momentSubmitting"
                    @change="onMomentImageChange"
                  />
                  <button type="button" class="dy-btn ghost small" :disabled="momentSubmitting" @click="pickMomentImage">
                    选择图片…
                  </button>
                  <span class="muted small file-chosen">{{ momentImageDisplay }}</span>
                  <button
                    v-if="momentImageFile"
                    type="button"
                    class="dy-btn ghost tiny"
                    :disabled="momentSubmitting"
                    @click="clearMomentImage"
                  >
                    清除
                  </button>
                </div>
              </label>
              <label class="form-field">
                <span class="field-label">CDP 地址</span>
                <input
                  v-model="momentForm.cdp_url"
                  placeholder="http://127.0.0.1:9222（勿填 localhost）"
                  :disabled="momentSubmitting"
                  class="dy-input"
                />
              </label>
              <div class="form-footer">
                <button
                  class="dy-btn primary large"
                  :disabled="momentSubmitting || !momentForm.content.trim()"
                  @click="submitMoment"
                >
                  <span v-if="momentSubmitting">⏳ 发布中…</span>
                  <span v-else>🌐 发布朋友圈</span>
                </button>
                <span v-if="momentSubmitMsg" class="hint" :class="{ ok: momentSubmitOk, err: !momentSubmitOk }">
                  {{ momentSubmitMsg }}
                </span>
              </div>
            </div>
          </div>

          <!-- 历史记录 -->
          <div class="card history-card">
            <div class="card-title">
              <span class="card-title-icon">📋</span> 发布历史
              <button class="icon-btn ml-auto" :class="{ spinning: momentsLoading }" @click="loadMoments">
                <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
                  <path d="M14.5 3A8 8 0 1 0 15 8h-1.5a6.5 6.5 0 1 1-.52-2.57L11 7h4V3l-1.5 1.5-.5-.5z"/>
                </svg>
              </button>
            </div>
            <div v-if="momentsLoading" class="empty-state small">加载中…</div>
            <div v-else-if="moments.length === 0" class="empty-state small">
              <div class="empty-icon">📭</div>
              <p>暂无发布记录</p>
            </div>
            <div v-else class="moment-list">
              <div v-for="m in moments" :key="m.id" class="moment-item">
                <div class="moment-meta">
                  <span class="status-tag" :class="m.status">{{ statusLabel(m.status) }}</span>
                  <span class="muted small">{{ fmtTime(m.created_at) }}</span>
                  <button v-if="m.status === 'running'" class="dy-btn ghost tiny" @click="pollMoment(m.id)">刷新</button>
                </div>
                <p class="moment-text">{{ m.content }}</p>
                <div v-if="m.image_path" class="muted small img-path">📎 {{ m.image_path }}</div>
                <details v-if="m.log_tail" class="log-block">
                  <summary class="muted small">查看日志</summary>
                  <pre class="log-pre">{{ m.log_tail }}</pre>
                </details>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>

    <!-- 录入弹窗 -->
    <div v-if="showAddModal" class="modal-mask" @click.self="showAddModal = false">
      <div class="modal-box">
        <div class="modal-header">
          <h3>手动录入线索</h3>
          <button class="icon-btn" @click="showAddModal = false">✕</button>
        </div>
        <div class="modal-body">
          <label class="form-field">
            <span class="field-label">手机号 <em>*</em></span>
            <input v-model="newLead.phone" placeholder="138xxxxxxxx" class="dy-input" />
          </label>
          <label class="form-field">
            <span class="field-label">来源</span>
            <select v-model="newLead.source" class="dy-select full">
              <option value="manual">手动录入</option>
              <option value="douyin">抖音</option>
              <option value="wxwork">企微</option>
            </select>
          </label>
          <label class="form-field">
            <span class="field-label">客户昵称</span>
            <input v-model="newLead.source_user" placeholder="可选" class="dy-input" />
          </label>
          <label class="form-field">
            <span class="field-label">对话背景</span>
            <textarea v-model="newLead.source_context" rows="3" placeholder="可选" class="dy-textarea" />
          </label>
        </div>
        <div class="modal-footer">
          <button class="dy-btn ghost" @click="showAddModal = false">取消</button>
          <button class="dy-btn primary" @click="submitNewLead">确认录入</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import {
  Bot,
  MessageSquare,
  Music2,
  Share2,
  UsersRound,
} from "lucide-vue-next";
import { computed, nextTick, onMounted, ref, watch } from "vue";

const API = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

// ── state ────────────────────────────────────────────────────────────────
const activeTab = ref("leads");
const loading = ref(false);

const leads = ref([]);
const leadsFilter = ref("");
const expandedLeadId = ref(null);

const threads = ref([]);
const convPlatformFilter = ref("douyin");
const threadSearch = ref("");
const selectedPeerKey = ref(null);
const selectedThread = ref(null);
const currentMessages = ref([]);
const streamEl = ref(null);

const dyArchives = ref([]);
const selectedDyArchive = ref("");
const dyImportLoading = ref(false);
const dyImportFeedback = ref("");
const dyImportFeedbackErr = ref(false);
const revaiClearLoading = ref(false);
const revaiClearFeedback = ref("");
const revaiClearFeedbackErr = ref(false);

const showAddModal = ref(false);
const newLead = ref({ phone: "", source: "manual", source_user: "", source_context: "" });

const moments = ref([]);
const momentsLoading = ref(false);
const momentSubmitting = ref(false);
const momentSubmitMsg = ref("");
const momentSubmitOk = ref(true);
const momentForm = ref({
  content: "",
  cdp_url: "http://127.0.0.1:9222",
});
const momentImageInputRef = ref(null);
const momentImageFile = ref(null);
const momentImageDisplay = computed(
  () => momentImageFile.value?.name ?? "未选择（仅支持常见图片格式）",
);

const wxExports = ref([]);
const wxExportLoading = ref(false);
const wxChatsLoading = ref(false);
const wxExportError = ref("");
const selectedExportName = ref("");
const wxChatsData = ref(null);
const wxConvSearch = ref("");
const selectedWxConv = ref(null);
const wxStreamEl = ref(null);

// ── tabs ─────────────────────────────────────────────────────────────────
const TABS = [
  { id: "leads", Icon: UsersRound, label: "客户线索", sub: "抖音/企微获客线索与添加任务", badge: () => leads.value.filter(l => l.status === "pending").length || null },
  { id: "conversations", Icon: Music2, label: "抖音对话记录", sub: "上报到控制台的即时会话（平台侧 SQLite）", badge: () => null },
  { id: "wx_export", Icon: MessageSquare, label: "企微对话记录", sub: "电脑客户端同步的归档会话内容", badge: () => null },
  { id: "moments", Icon: Share2, label: "发朋友圈", sub: "群发企业朋友圈内容", badge: () => moments.value.filter(m => m.status === "running").length || null },
];
const currentTab = computed(() => TABS.find(t => t.id === activeTab.value));

// ── computed ──────────────────────────────────────────────────────────────
const filteredLeads = computed(() => {
  if (!leadsFilter.value) return leads.value;
  return leads.value.filter(l => l.status === leadsFilter.value);
});
const filteredThreads = computed(() => {
  let list = threads.value;
  if (convPlatformFilter.value) list = list.filter(t => t.platform === convPlatformFilter.value);
  const q = threadSearch.value.trim().toLowerCase();
  if (q) list = list.filter(t => (t.contact_name || t.peer_key).toLowerCase().includes(q));
  return list;
});

const filteredWxConversations = computed(() => {
  const list = wxChatsData.value?.conversations || [];
  const q = wxConvSearch.value.trim().toLowerCase();
  if (!q) return list;
  return list.filter(
    c =>
      String(c.title || "").toLowerCase().includes(q) ||
      String(c.list_preview || "").toLowerCase().includes(q)
  );
});
const leadStats = computed(() => [
  { label: "待处理", val: leads.value.filter(l => l.status === "pending").length,  cls: "pending" },
  { label: "执行中", val: leads.value.filter(l => l.status === "running").length,  cls: "running" },
  { label: "已成功", val: leads.value.filter(l => l.status === "done").length,     cls: "done"    },
  { label: "失败",   val: leads.value.filter(l => l.status === "failed").length,   cls: "failed"  },
]);

// ── api ───────────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API}${path}`, opts);
  if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail || `HTTP ${res.status}`); }
  return res.json();
}
async function apiPost(path, data) {
  return apiFetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
}

// ── load ──────────────────────────────────────────────────────────────────
async function loadLeads()   { leads.value   = await apiFetch("/api/leads"); }
async function loadThreads() { threads.value = await apiFetch("/api/conversation_threads"); }

async function loadDouyinArchives() {
  try {
    dyArchives.value = await apiFetch("/api/import/douyin-archives");
    dyImportFeedbackErr.value = false;
    dyImportFeedback.value = "";
    const folders = new Set(dyArchives.value.map(a => a.folder));
    if (selectedDyArchive.value && !folders.has(selectedDyArchive.value)) selectedDyArchive.value = "";
  } catch (e) {
    dyArchives.value = [];
    dyImportFeedbackErr.value = true;
    dyImportFeedback.value = e.message || String(e);
  }
}

async function importDouyinToSqlite() {
  dyImportLoading.value = true;
  dyImportFeedbackErr.value = false;
  dyImportFeedback.value = "";
  try {
    const body = selectedDyArchive.value ? { folder: selectedDyArchive.value } : {};
    const r = await apiPost("/api/import/douyin-recent-chats", body);
    const ins = r.inserted_messages ?? r.inserted ?? 0;
    const sk = r.skipped_rows ?? r.skipped ?? 0;
    dyImportFeedback.value = `已从 ${r.archive || selectedDyArchive.value || "最新"} 写入 ${ins} 条，跳过 ${sk} 条。`;
    await loadThreads();
    if (selectedPeerKey.value) await loadMessages(selectedPeerKey.value);
  } catch (e) {
    dyImportFeedbackErr.value = true;
    dyImportFeedback.value = e.message || String(e);
  } finally {
    dyImportLoading.value = false;
  }
}

function currentDouyinSessionId() {
  const raw = selectedThread.value?.contact_name || selectedThread.value?.contact_id || selectedThread.value?.peer_key || "ltx";
  const ident = String(raw).includes(":") ? String(raw).split(":").pop() : String(raw);
  return `douyin-${ident || "ltx"}`.replace(/[^\w.-]+/g, "_").slice(0, 128);
}

async function clearRevaiSession() {
  revaiClearLoading.value = true;
  revaiClearFeedback.value = "";
  revaiClearFeedbackErr.value = false;
  const sessionId = currentDouyinSessionId();
  try {
    const r = await apiPost("/api/douyin/revai/session/clear", { session_id: sessionId });
    const msgRows = r.deleted_message_rows ?? 0;
    const sessionRows = r.deleted_session_rows ?? 0;
    revaiClearFeedback.value = `已清理 ${r.session_id || sessionId}：session ${sessionRows}，消息 ${msgRows}`;
  } catch (e) {
    revaiClearFeedbackErr.value = true;
    revaiClearFeedback.value = e.message || String(e);
  } finally {
    revaiClearLoading.value = false;
  }
}
async function loadMoments() {
  momentsLoading.value = true;
  try {
    moments.value = await apiFetch("/api/moments");
  } catch(e) { console.warn(e); } finally { momentsLoading.value = false; }
}

function absoluteExportUrl(path) {
  if (path == null || path === "") return "";
  const s = String(path);
  if (/^https?:\/\//i.test(s)) return s;
  const base = API.replace(/\/$/, "");
  return base + (s.startsWith("/") ? s : `/${s}`);
}

function exportScreenshotUrl(raw) {
  if (!raw) return "";
  let s = String(raw).trim().replace(/\\/g, "/");
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("/exports/")) return absoluteExportUrl(s);
  if (s.toLowerCase().startsWith("exports/")) return absoluteExportUrl(`/${s}`);
  return absoluteExportUrl(`/exports/${s.replace(/^exports\/?/i, "")}`);
}

function wxBubbleAlign(m) {
  if (m.kind === "time") return "center";
  const d = m.direction || "";
  if (d === "outgoing") return "out";
  if (d === "incoming") return "in";
  return "in";
}

async function loadWxChatsSnapshot(name) {
  if (!name) {
    wxChatsData.value = null;
    selectedWxConv.value = null;
    return;
  }
  wxChatsLoading.value = true;
  wxExportError.value = "";
  try {
    const q = encodeURIComponent(name);
    wxChatsData.value = await apiFetch(`/api/wxwork/chats?export=${q}`);
    const list = wxChatsData.value?.conversations || [];
    selectedWxConv.value = list.length ? list[0] : null;
    await nextTick();
    if (wxStreamEl.value) wxStreamEl.value.scrollTop = wxStreamEl.value.scrollHeight;
  } catch (e) {
    wxChatsData.value = null;
    selectedWxConv.value = null;
    wxExportError.value = e.message || String(e);
  } finally {
    wxChatsLoading.value = false;
  }
}

async function loadWxExportsOnly() {
  wxExportLoading.value = true;
  wxExportError.value = "";
  try {
    wxExports.value = await apiFetch("/api/wxwork/exports");
    const names = new Set(wxExports.value.map(x => x.name));
    if (!wxExports.value.length) {
      selectedExportName.value = "";
      wxChatsData.value = null;
      selectedWxConv.value = null;
      return;
    }
    if (!wxExports.value.length) {
      selectedExportName.value = "";
      wxChatsData.value = null;
      selectedWxConv.value = null;
      return;
    }
    if (!selectedExportName.value || !names.has(selectedExportName.value))
      selectedExportName.value = wxExports.value[0].name;

    await loadWxChatsSnapshot(selectedExportName.value);
  } catch (e) {
    wxExports.value = [];
    wxExportError.value = e.message || String(e);
  } finally {
    wxExportLoading.value = false;
  }
}

async function selectWxConversation(c) {
  selectedWxConv.value = c;
  await nextTick();
  if (wxStreamEl.value) wxStreamEl.value.scrollTop = wxStreamEl.value.scrollHeight;
}

async function onWxBatchChanged() {
  if (!selectedExportName.value) return;
  await loadWxChatsSnapshot(selectedExportName.value);
}

async function refreshAll() {
  loading.value = true;
  try {
    await Promise.all([loadLeads(), loadThreads()]);
    if (selectedPeerKey.value) await loadMessages(selectedPeerKey.value);
    if (activeTab.value === "wx_export") await loadWxExportsOnly();
  } finally { loading.value = false; }
}
async function loadMessages(peerKey) {
  currentMessages.value = await apiFetch(`/api/conversations?peer_key=${encodeURIComponent(peerKey)}&limit=500`);
  await nextTick();
  if (streamEl.value) streamEl.value.scrollTop = streamEl.value.scrollHeight;
}

// ── actions ───────────────────────────────────────────────────────────────
async function triggerLead(id) {
  try { await apiPost(`/api/leads/${id}/trigger`, {}); await loadLeads(); } catch(e) { alert(`触发失败：${e.message}`); }
}
async function resetLead(id) {
  try { await apiPost(`/api/leads/${id}/reset`, {}); await loadLeads(); } catch(e) { alert(`重置失败：${e.message}`); }
}
function toggleLeadExpand(id) { expandedLeadId.value = expandedLeadId.value === id ? null : id; }
function openAddLeadModal() { newLead.value = { phone: "", source: "manual", source_user: "", source_context: "" }; showAddModal.value = true; }
async function submitNewLead() {
  if (!newLead.value.phone) { alert("请输入手机号"); return; }
  try { await apiPost("/api/leads", newLead.value); showAddModal.value = false; await loadLeads(); } catch(e) { alert(`录入失败：${e.message}`); }
}
async function seedDemo() {
  try { const r = await apiPost("/api/seed-demo", {}); alert(`演示数据注入成功：${r.leads.join(", ")}`); await refreshAll(); } catch(e) { alert(`失败：${e.message}`); }
}
async function selectThread(t) { selectedThread.value = t; selectedPeerKey.value = t.peer_key; await loadMessages(t.peer_key); }

function pickMomentImage() {
  momentImageInputRef.value?.click();
}
function onMomentImageChange(ev) {
  const f = ev.target?.files?.[0];
  momentImageFile.value = f ?? null;
}
function clearMomentImage() {
  momentImageFile.value = null;
  if (momentImageInputRef.value) momentImageInputRef.value.value = "";
}

async function submitMoment() {
  if (!momentForm.value.content.trim()) return;
  momentSubmitting.value = true;
  momentSubmitMsg.value = "";
  try {
    const fd = new FormData();
    fd.append("content", momentForm.value.content.trim());
    fd.append("cdp_url", momentForm.value.cdp_url.trim() || "http://127.0.0.1:9222");
    if (momentImageFile.value) fd.append("image", momentImageFile.value);
    const res = await fetch(`${API}/api/moments`, { method: "POST", body: fd });
    const b = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = b.detail;
      const msg =
        typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg || x).join("; ") : JSON.stringify(d);
      throw new Error(msg || `HTTP ${res.status}`);
    }
    momentSubmitOk.value = true;
    momentSubmitMsg.value = "已提交，后台执行中…";
    momentForm.value.content = "";
    clearMomentImage();
    await loadMoments();
    setTimeout(loadMoments, 30_000);
  } catch (e) {
    momentSubmitOk.value = false;
    momentSubmitMsg.value = `提交失败：${e.message}`;
  } finally {
    momentSubmitting.value = false;
  }
}
async function pollMoment(id) {
  try { const m = await apiFetch(`/api/moments/${id}`); const i = moments.value.findIndex(x => x.id === id); if (i >= 0) moments.value[i] = m; } catch(e) {}
}

// ── format ────────────────────────────────────────────────────────────────
function maskPhone(p) { if (!p || p.length < 7) return p; return p.slice(0,3) + "****" + p.slice(-4); }
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d.getTime())) return iso;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }) + " " + d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
const STATUS_MAP = { pending: "待处理", running: "执行中", done: "已成功", failed: "失败" };
function statusLabel(s) { return STATUS_MAP[s] || s; }

watch(activeTab, async (tab) => {
  loading.value = true;
  try {
    if (tab === "leads") await loadLeads();
    else if (tab === "conversations") {
      await loadThreads();
      await loadDouyinArchives();
    }
    else if (tab === "wx_export") await loadWxExportsOnly();
    else if (tab === "moments") await loadMoments();
  } finally { loading.value = false; }
});
onMounted(() => refreshAll());
</script>

<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --sidebar-bg: #0D1F3C;
  --sidebar-hover: rgba(255,255,255,.07);
  --sidebar-active: rgba(22,100,255,.2);
  --sidebar-active-bar: #1664FF;
  --sidebar-text: rgba(255,255,255,.65);
  --sidebar-text-active: #fff;
  --sidebar-width: 200px;

  --bg: #F0F2F5;
  --card-bg: #fff;
  --border: #E8EAED;
  --border-light: #F0F2F5;
  --header-bg: #fff;

  --blue: #1664FF;
  --blue-hover: #0F52E0;
  --blue-light: #EBF1FF;
  --text: #1A1A1A;
  --muted: #8C8C8C;
  --muted-bg: #F5F6FA;

  --green: #00B365;
  --green-bg: #E8FAF2;
  --orange: #FF7D00;
  --orange-bg: #FFF3E8;
  --red: #E34D59;
  --red-bg: #FEEFF0;
  --purple: #7B61FF;
  --purple-bg: #F0EDFF;

  --radius: 8px;
  --radius-lg: 12px;
  --shadow: 0 1px 4px rgba(0,0,0,.08);
  --shadow-md: 0 2px 12px rgba(0,0,0,.1);

  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
  font-size: 14px;
}

body { background: var(--bg); color: var(--text); height: 100vh; overflow: hidden; }
#app { height: 100vh; }

/* ── App shell ── */
.app-shell { display: flex; height: 100vh; overflow: hidden; }

/* ── Sidebar ── */
.sidebar {
  width: var(--sidebar-width); flex-shrink: 0;
  background: var(--sidebar-bg);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.sidebar-logo {
  display: flex; align-items: center; gap: 10px;
  padding: 20px 16px 16px;
  border-bottom: 1px solid rgba(255,255,255,.08);
  color: rgba(255,255,255,.94);
}
.logo-icon { display: inline-flex; flex-shrink: 0; opacity: .95; align-items: center; justify-content: center; }
.logo-icon svg { display: block; }
.logo-text { font-size: 15px; font-weight: 700; color: #fff; letter-spacing: .3px; }
.sidebar-menu { flex: 1; padding: 8px 0; overflow-y: auto; }
.menu-item {
  width: 100%; display: flex; align-items: center; gap: 10px;
  padding: 11px 16px; border: none; background: none; cursor: pointer;
  color: var(--sidebar-text); font-size: 14px; text-align: left;
  transition: all .15s; position: relative; border-radius: 0;
}
.menu-item:hover { background: var(--sidebar-hover); color: #fff; }
.menu-item.active { background: var(--sidebar-active); color: var(--sidebar-text-active); }
.menu-item.active::before {
  content: ""; position: absolute; left: 0; top: 50%; transform: translateY(-50%);
  width: 3px; height: 60%; background: var(--sidebar-active-bar); border-radius: 0 2px 2px 0;
}
.menu-icon {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  opacity: .95;
}
.menu-icon svg { display: block; }
.menu-label { flex: 1; }
.menu-badge {
  background: var(--blue); color: #fff; font-size: 11px; font-weight: 700;
  padding: 1px 6px; border-radius: 10px; min-width: 18px; text-align: center;
}

/* ── Content area ── */
.content-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }
.content-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 56px; flex-shrink: 0;
  background: var(--header-bg); border-bottom: 1px solid var(--border);
  box-shadow: var(--shadow);
}
.header-left { display: flex; align-items: baseline; gap: 10px; }
.page-title { font-size: 16px; font-weight: 700; color: var(--text); }
.page-sub { font-size: 12px; color: var(--muted); }
.icon-btn {
  width: 32px; height: 32px; border: 1px solid var(--border); border-radius: 6px;
  background: none; cursor: pointer; color: var(--muted); display: flex; align-items: center; justify-content: center;
  transition: all .15s;
}
.icon-btn:hover { border-color: var(--blue); color: var(--blue); }
.icon-btn.spinning svg { animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.ml-auto { margin-left: auto; }

/* ── Page body ── */
.page-body { flex: 1; overflow-y: auto; padding: 20px 24px; }

/* ── Toolbar ── */
.toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
.toolbar-left { display: flex; align-items: center; gap: 8px; }
.toolbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.toolbar-right .dy-select {
  width: auto;
  min-width: 130px;
  max-width: 200px;
}
.stat-pills { display: flex; gap: 6px; }
.stat-pill {
  font-size: 12px; padding: 4px 10px; border-radius: 6px; display: flex; align-items: center; gap: 5px;
  background: var(--muted-bg); color: var(--muted);
}
.stat-pill b { font-weight: 700; }
.stat-pill.pending { background: var(--muted-bg); color: var(--muted); }
.stat-pill.running { background: var(--blue-light); color: var(--blue); }
.stat-pill.done    { background: var(--green-bg);   color: var(--green); }
.stat-pill.failed  { background: var(--red-bg);     color: var(--red); }

/* ── Card ── */
.card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius-lg); box-shadow: var(--shadow); overflow: hidden;
}
.card-title {
  display: flex; align-items: center; gap: 8px;
  padding: 16px 20px; font-size: 14px; font-weight: 700;
  border-bottom: 1px solid var(--border-light);
}
.card-title-icon { font-size: 16px; }

/* ── Table ── */
.dy-table { width: 100%; border-collapse: collapse; }
.dy-table th {
  padding: 10px 16px; text-align: left; font-size: 12px; font-weight: 600;
  color: var(--muted); background: var(--muted-bg);
  border-bottom: 1px solid var(--border);
}
.dy-table td { padding: 12px 16px; border-bottom: 1px solid var(--border-light); vertical-align: middle; }
.dy-row { cursor: pointer; transition: background .1s; }
.dy-row:hover { background: #FAFBFF; }
.dy-row.expanded { background: var(--blue-light); }
.dy-row:last-child td { border-bottom: none; }

/* ── Expand panel ── */
.expand-tr td { padding: 0; background: #FAFBFF; }
.expand-panel { padding: 12px 20px 16px 36px; border-top: 1px solid var(--border-light); }
.ctx-text { font-size: 13px; color: var(--muted); margin-bottom: 10px; }
.steps { display: flex; flex-direction: column; gap: 4px; }
.step {
  display: flex; align-items: center; gap: 8px; font-size: 13px;
  padding: 6px 10px; border-radius: 6px;
}
.step.ok  { background: var(--green-bg); }
.step.fail{ background: var(--red-bg); }
.step-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.step.ok   .step-dot { background: var(--green); }
.step.fail .step-dot { background: var(--red); }
.step-name { font-weight: 600; min-width: 100px; }
.step-time { font-size: 11px; color: var(--muted); margin-left: auto; }
.step-err  { font-size: 11px; color: var(--red); max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.step-link { font-size: 11px; color: var(--blue); text-decoration: none; }
.step-link:hover { text-decoration: underline; }

/* ── Tags ── */
.platform-tag {
  font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 5px;
  display: inline-block;
}
.platform-tag.douyin { background: rgba(255,64,128,.1); color: #E8255A; }
.platform-tag.wxwork { background: var(--green-bg); color: var(--green); }
.platform-tag.manual { background: var(--blue-light); color: var(--blue); }

.status-tag {
  font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 5px; display: inline-block;
}
.status-tag.pending { background: var(--muted-bg);   color: var(--muted);  }
.status-tag.running { background: var(--blue-light); color: var(--blue);   }
.status-tag.done    { background: var(--green-bg);   color: var(--green);  }
.status-tag.failed  { background: var(--red-bg);     color: var(--red);    }

/* ── Buttons ── */
.dy-btn {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 7px 16px; border-radius: 6px; border: none; cursor: pointer;
  font-size: 13px; font-weight: 600; transition: all .15s; line-height: 1;
}
.dy-btn.primary { background: var(--blue); color: #fff; }
.dy-btn.primary:hover:not(:disabled) { background: var(--blue-hover); }
.dy-btn.primary:disabled { opacity: .5; cursor: not-allowed; }
.dy-btn.secondary { background: var(--blue-light); color: var(--blue); border: 1px solid rgba(22,100,255,.3); }
.dy-btn.secondary:hover { background: #DDE9FF; }
.dy-btn.ghost { background: none; color: var(--muted); border: 1px solid var(--border); }
.dy-btn.ghost:hover { border-color: var(--blue); color: var(--blue); }
.dy-btn.small { padding: 4px 10px; font-size: 12px; }
.dy-btn.tiny  { padding: 2px 7px;  font-size: 11px; }
.dy-btn.large { padding: 10px 24px; font-size: 14px; }

/* ── Form inputs ── */
.dy-input, .dy-select, .dy-textarea {
  width: 100%; padding: 8px 12px; border-radius: 6px;
  border: 1px solid var(--border); background: #fff; color: var(--text);
  font-size: 13px; outline: none; transition: border .15s;
  font-family: inherit;
}
.dy-input:focus, .dy-select:focus, .dy-textarea:focus { border-color: var(--blue); }
.dy-input:disabled, .dy-textarea:disabled { background: var(--muted-bg); color: var(--muted); }
.dy-textarea { resize: vertical; }
.dy-select.full { width: 100%; }

/* ── Chat layout ── */
.chat-layout { padding: 0 !important; display: flex; overflow: hidden; }
.chat-sidebar {
  width: 260px; flex-shrink: 0; border-right: 1px solid var(--border);
  background: var(--card-bg); display: flex; flex-direction: column;
}
.chat-sidebar-header { padding: 12px; display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.dy-import-strip {
  padding-top: 4px;
  border-top: 1px dashed var(--border);
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dy-import-label { display: flex; flex-direction: column; gap: 4px; }
.dy-import-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.dy-import-feedback { font-size: 12px; color: var(--muted); line-height: 1.35; }
.dy-import-feedback.err { color: #c62828; }
.chat-thread-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.thread-item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 12px 14px; border: none; background: none; text-align: left; cursor: pointer;
  border-bottom: 1px solid var(--border-light); transition: background .1s; width: 100%; color: var(--text);
}
.thread-item:hover { background: var(--muted-bg); }
.thread-item.active { background: var(--blue-light); }
.avatar {
  width: 36px; height: 36px; border-radius: 50%; display: flex;
  align-items: center; justify-content: center; font-weight: 700; font-size: 13px; flex-shrink: 0;
}
.avatar.douyin { background: rgba(255,64,128,.15); color: #E8255A; }
.avatar.wxwork  { background: var(--green-bg); color: var(--green); }
.avatar.lg      { width: 40px; height: 40px; font-size: 15px; }
.thread-info { flex: 1; min-width: 0; }
.thread-row  { display: flex; justify-content: space-between; margin-bottom: 3px; }
.thread-name { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thread-time { font-size: 11px; color: var(--muted); flex-shrink: 0; margin-left: 6px; }
.thread-preview { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.chat-main { flex: 1; display: flex; flex-direction: column; background: var(--bg); }
.chat-header {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 20px; background: var(--card-bg); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.chat-header h3 { font-size: 15px; font-weight: 700; }
.chat-header-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  min-width: 0;
}
.chat-header-actions .dy-import-feedback {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.message-stream { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 8px; }
.msg-row { display: flex; }
.msg-row.out { justify-content: flex-end; }
.bubble {
  max-width: 60%; padding: 10px 14px; border-radius: 12px;
  background: var(--card-bg); font-size: 14px; line-height: 1.5;
  box-shadow: var(--shadow); border: 1px solid var(--border);
}
.msg-row.out .bubble { background: var(--blue); color: #fff; border-color: var(--blue); }
.msg-row.out .bubble .msg-time { color: rgba(255,255,255,.7); }
.msg-time { display: block; font-size: 11px; color: var(--muted); margin-top: 4px; text-align: right; }
.chat-ph { display: flex; flex-direction: column; align-items: center; justify-content: center; flex: 1; }

/* ── 企微对话记录（归档） ── */
.wx-export-layout {
  flex-direction: column !important;
  padding: 16px 20px !important;
  gap: 12px;
  min-height: 0;
}
.wx-export-toolbar.card.flat {
  flex-shrink: 0;
  box-shadow: var(--shadow);
  margin: 0;
}
.wx-toolbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}
.inline-field {
  flex: 1;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wx-export-select {
  width: auto;
  min-width: min(520px, 100%);
}
.wx-meta { margin-top: 8px; line-height: 1.45; }
.wx-meta.err { color: var(--red); font-weight: 600; }

.wx-chat-split {
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--card-bg);
  box-shadow: var(--shadow);
}

.avatar.wx {
  background: var(--green-bg);
  color: var(--green);
}

.msg-row.wx-time-row {
  justify-content: center;
  margin: 6px 0;
}
.time-chip-wx {
  font-size: 12px;
  color: var(--muted);
  background: var(--muted-bg);
  padding: 4px 12px;
  border-radius: 999px;
}
.msg-row.wx-msg-row.in { justify-content: flex-start; }
.msg-row.wx-msg-row.out { justify-content: flex-end; }
.msg-row.wx-msg-row.center { justify-content: center; }

.conv-screens-row {
  padding: 8px 20px;
  border-bottom: 1px solid var(--border-light);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  background: var(--card-bg);
}
.screenshot-thumb-link {
  display: block;
  line-height: 0;
}
.screenshot-thumb {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--border);
}
.wx-msg-sender { margin-bottom: 4px; }
.msg-foot { margin-top: 6px; display: block; }
.screenshot-mini {
  font-size: 11px;
  color: var(--blue);
  margin-top: 8px;
  display: inline-block;
}

/* ── Moments ── */
.two-col { display: grid; grid-template-columns: minmax(280px, 420px) 1fr; gap: 20px; align-items: start; max-width: 1200px; }
@media (max-width: 900px) {
  .two-col { grid-template-columns: 1fr; }
}
.form-card .form-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.form-field { display: flex; flex-direction: column; gap: 5px; }
.file-picker-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.file-input-sr {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  overflow: hidden;
  pointer-events: none;
}
.file-chosen {
  flex: 1;
  min-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.field-label { font-size: 12px; font-weight: 600; color: var(--muted); }
.field-label em { color: var(--red); font-style: normal; }
.form-footer { display: flex; align-items: center; gap: 12px; padding-top: 4px; }
.hint { font-size: 13px; }
.hint.ok  { color: var(--green); }
.hint.err { color: var(--red); }

.moment-list { display: flex; flex-direction: column; }
.moment-item { padding: 14px 20px; border-bottom: 1px solid var(--border-light); }
.moment-item:last-child { border-bottom: none; }
.moment-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.moment-meta .muted { margin-left: auto; }
.moment-text { font-size: 14px; line-height: 1.6; color: var(--text); white-space: pre-wrap; word-break: break-word; }
.img-path { margin-top: 4px; }
.log-block { margin-top: 8px; }
.log-block summary { cursor: pointer; user-select: none; }
.log-pre {
  margin-top: 6px; padding: 10px; border-radius: 6px;
  background: var(--muted-bg); border: 1px solid var(--border);
  font-size: 11px; line-height: 1.5; max-height: 180px;
  overflow: auto; white-space: pre-wrap; word-break: break-all;
}

/* ── Modal ── */
.modal-mask {
  position: fixed; inset: 0; background: rgba(0,0,0,.4);
  display: flex; align-items: center; justify-content: center; z-index: 200;
  backdrop-filter: blur(2px);
}
.modal-box {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius-lg); width: 440px; max-width: 92vw;
  box-shadow: var(--shadow-md);
}
.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 20px 14px; border-bottom: 1px solid var(--border);
}
.modal-header h3 { font-size: 15px; font-weight: 700; }
.modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
.modal-footer { display: flex; justify-content: flex-end; gap: 10px; padding: 12px 20px; border-top: 1px solid var(--border); }

/* ── Misc ── */
.empty-state { text-align: center; padding: 48px 0; color: var(--muted); }
.empty-state.small { padding: 28px 0; font-size: 13px; }
.empty-icon { font-size: 32px; margin-bottom: 8px; }
.mono { font-family: "SF Mono", Consolas, monospace; font-size: 13px; letter-spacing: .4px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.actions-cell { white-space: nowrap; }
</style>
