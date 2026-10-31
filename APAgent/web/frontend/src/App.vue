<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">AP</div>
        <div>
          <h1>APAgent</h1>
          <span>下单管理后台</span>
        </div>
      </div>
      <nav class="nav">
        <button v-for="item in navItems" :key="item.key" :class="{ active: page === item.key }" @click="page = item.key">
          {{ item.label }}
        </button>
      </nav>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div>
          <h2>{{ currentPage.label }}</h2>
          <p>{{ currentPage.desc }}</p>
        </div>
        <div class="top-actions">
          <span :class="['health', healthStatus]">{{ healthText }}</span>
          <button @click="loadAll">刷新</button>
        </div>
      </header>

      <section v-if="page === 'dashboard'" class="page-stack">
        <div class="metrics">
          <div class="metric"><span>SKU 数量</span><strong>{{ skus.length }}</strong></div>
          <div class="metric"><span>信息套件</span><strong>{{ identities.length }}</strong></div>
          <div class="metric"><span>任务总数</span><strong>{{ tasks.length }}</strong></div>
          <div class="metric"><span>执行中</span><strong>{{ runningCount }}</strong></div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <div>
              <h3>最近任务</h3>
              <p>展示最新创建的下单任务。</p>
            </div>
            <button class="primary" @click="openTaskModal()">新建任务</button>
          </div>
          <TaskTable :tasks="tasks.slice(0, 8)" @edit="openTaskModal" @delete="deleteTask" @run="runTask" @select="selectTask" />
        </div>
      </section>

      <section v-else-if="page === 'skus'" class="panel page-panel">
        <div class="panel-head">
          <div>
            <h3>SKU 管理</h3>
            <p>共 {{ skus.length }} 条记录，规格选项支持人工修订。</p>
          </div>
          <button class="primary" @click="openSkuModal()">新建 SKU</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="id-col">ID</th>
                <th>名称</th>
                <th>机型</th>
                <th>容量</th>
                <th>颜色</th>
                <th>连接方式</th>
                <th>Part Number</th>
                <th>状态</th>
                <th class="op-col">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="sku in skus" :key="sku.id">
                <td>{{ sku.id }}</td>
                <td class="strong">{{ sku.name }}</td>
                <td>{{ modelLabel(sku.model_path) }}</td>
                <td>{{ sku.storage || "-" }}</td>
                <td>{{ sku.color || "-" }}</td>
                <td>{{ carrierLabel(sku.carrier) }}</td>
                <td>{{ sku.part_number }}</td>
                <td><span :class="['status-pill', sku.enabled ? 'done' : 'muted']">{{ sku.enabled ? "启用" : "停用" }}</span></td>
                <td class="ops">
                  <button @click="openSkuModal(sku)">编辑</button>
                  <button class="danger" @click="deleteSku(sku)">删除</button>
                </td>
              </tr>
              <tr v-if="!skus.length"><td colspan="9" class="empty">暂无数据</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-else-if="page === 'identities'" class="panel page-panel">
        <div class="panel-head">
          <div>
            <h3>人工信息管理</h3>
            <p>维护收货人、联系方式和付款信息，可配置多套资料。</p>
          </div>
          <button class="primary" @click="openIdentityModal()">新建信息</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="id-col">ID</th>
                <th>名称</th>
                <th>联系人</th>
                <th>邮箱</th>
                <th>电话</th>
                <th>地址</th>
                <th>信用卡</th>
                <th>状态</th>
                <th class="op-col">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in identities" :key="item.id">
                <td>{{ item.id }}</td>
                <td class="strong">{{ item.name }}</td>
                <td>{{ item.first_name }} {{ item.last_name }}</td>
                <td>{{ item.email }}</td>
                <td>{{ item.phone || "-" }}</td>
                <td>{{ item.street }}, {{ item.city }}, {{ item.state }} {{ item.zip_code }}</td>
                <td>{{ item.card_number_masked || "未配置" }}</td>
                <td><span :class="['status-pill', item.enabled ? 'done' : 'muted']">{{ item.enabled ? "启用" : "停用" }}</span></td>
                <td class="ops">
                  <button @click="openIdentityModal(item)">编辑</button>
                  <button class="danger" @click="deleteIdentity(item)">删除</button>
                </td>
              </tr>
              <tr v-if="!identities.length"><td colspan="9" class="empty">暂无数据</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-else-if="page === 'tasks'" class="grid task-layout">
        <div class="panel page-panel">
          <div class="panel-head">
            <div>
              <h3>下单任务</h3>
              <p>任务列表与执行进度合并展示，点击任务查看右侧进度。</p>
            </div>
            <button class="primary" @click="openTaskModal()">新建任务</button>
          </div>
          <TaskTable :tasks="tasks" @edit="openTaskModal" @delete="deleteTask" @run="runTask" @select="selectTask" />
        </div>
        <div class="panel progress-panel">
          <div class="panel-head">
            <div>
              <h3>执行进度</h3>
              <p>{{ selectedTask ? `任务 #${selectedTask.id}` : "未选择任务" }}</p>
            </div>
            <button :disabled="!selectedTask || selectedTask.status === 'running'" @click="runTask(selectedTask)">执行</button>
          </div>
          <div v-if="selectedTask" :class="['stage', selectedTask.status]">
            <strong>{{ statusLabel(selectedTask.status) }}</strong>
            <span>{{ stageLabel(selectedTask.current_stage) }}</span>
            <span>支付方式：{{ paymentLabel(selectedTask.payment_method) }}</span>
            <span v-if="selectedTask.error">{{ selectedTask.error }}</span>
            <a v-if="selectedTask.review_url" :href="selectedTask.review_url" target="_blank">查看页面</a>
          </div>
          <div class="timeline">
            <div v-for="(log, idx) in taskDetail.logs || []" :key="idx" class="log-line">
              <i>{{ log.time }}</i>
              <strong>{{ stageLabel(log.stage) }}</strong>
              <span>{{ log.message }}</span>
            </div>
            <div v-if="!(taskDetail.logs || []).length" class="empty timeline-empty">暂无执行日志</div>
          </div>
        </div>
      </section>

      <section v-else-if="page === 'prices'" class="panel page-panel">
        <div class="panel-head">
          <div>
            <h3>SKU 价格表</h3>
            <p>展示最近一次遍历采集的配置价格，数据由采集脚本写入。</p>
          </div>
          <button @click="loadPrices">刷新价格</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="id-col">ID</th>
                <th>商品</th>
                <th>容量</th>
                <th>颜色</th>
                <th>连接方式</th>
                <th>价格</th>
                <th>采集时间</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="record in priceRecords" :key="record.id">
                <td>{{ record.id }}</td>
                <td class="strong">{{ record.model }}</td>
                <td>{{ record.storage || "-" }}</td>
                <td>{{ record.color || "-" }}</td>
                <td>{{ record.carrier || "-" }}</td>
                <td>{{ record.price_text || "-" }}</td>
                <td>{{ record.captured_at }}</td>
                <td><span :class="['status-pill', record.ok ? 'done' : 'failed']">{{ record.ok ? "成功" : "失败" }}</span></td>
              </tr>
              <tr v-if="!priceRecords.length"><td colspan="8" class="empty">暂无价格数据</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <div v-if="modal.open" class="modal-mask" @click.self="closeModal">
      <div class="modal">
        <div class="modal-head">
          <h3>{{ modalTitle }}</h3>
          <button class="icon-btn" @click="closeModal">×</button>
        </div>

        <div v-if="modal.type === 'sku'" class="form-grid">
          <label class="wide">名称<input v-model="skuForm.name" /></label>
          <label>机型<input v-model="skuForm.model_path" list="model-options" /></label>
          <label>容量<input v-model="skuForm.storage" list="storage-options" /></label>
          <label>颜色<input v-model="skuForm.color" list="color-options" /></label>
          <label>连接方式<input v-model="skuForm.carrier" list="carrier-options" /></label>
          <label>Part Number<input v-model="skuForm.part_number" /></label>
          <label>AppleCare<input v-model="skuForm.applecare" list="applecare-options" /></label>
          <label>付款选项<input v-model="skuForm.payment_option" list="payment-options" /></label>
          <label>状态<select v-model="skuForm.enabled"><option :value="true">启用</option><option :value="false">停用</option></select></label>
          <label class="wide">选项配置 JSON<textarea v-model="skuForm.option_config" spellcheck="false"></textarea></label>
          <datalist id="model-options"><option value="6.9-inch-display" /><option value="6.3-inch-display" /><option value="homepod-mini" /></datalist>
          <datalist id="storage-options"><option value="256gb" /><option value="512gb" /><option value="1tb" /><option value="2tb" /></datalist>
          <datalist id="color-options"><option value="silver" /><option value="cosmic-orange" /><option value="deep-blue" /><option value="white" /></datalist>
          <datalist id="carrier-options"><option value="unlocked" /><option value="att" /><option value="t-mobile" /><option value="verizon" /><option value="boost-mobile" /></datalist>
          <datalist id="applecare-options"><option value="none" /><option value="theft_loss" /><option value="one" /></datalist>
          <datalist id="payment-options"><option value="fullPrice" /></datalist>
        </div>

        <div v-else-if="modal.type === 'identity'" class="form-grid">
          <label>名称<input v-model="identityForm.name" /></label>
          <label>First Name<input v-model="identityForm.first_name" /></label>
          <label>Last Name<input v-model="identityForm.last_name" /></label>
          <label>Email<input v-model="identityForm.email" /></label>
          <label>Phone<input v-model="identityForm.phone" /></label>
          <label class="wide">Street<input v-model="identityForm.street" /></label>
          <label>Apt/Suite<input v-model="identityForm.street2" /></label>
          <label>City<input v-model="identityForm.city" /></label>
          <label>State<input v-model="identityForm.state" /></label>
          <label>ZIP<input v-model="identityForm.zip_code" /></label>
          <label class="wide">Card Number<input v-model="identityForm.card_number" type="password" autocomplete="off" placeholder="编辑时留空表示不变" /></label>
          <label>Exp Month<input v-model="identityForm.exp_month" /></label>
          <label>Exp Year<input v-model="identityForm.exp_year" /></label>
          <label>CVV<input v-model="identityForm.cvv" type="password" autocomplete="off" placeholder="留空不变" /></label>
          <label>状态<select v-model="identityForm.enabled"><option :value="true">启用</option><option :value="false">停用</option></select></label>
        </div>

        <div v-else-if="modal.type === 'task'" class="form-grid single">
          <label>任务名称<input v-model="taskForm.name" /></label>
          <label>SKU<select v-model.number="taskForm.sku_id"><option v-for="sku in skus" :key="sku.id" :value="sku.id">{{ sku.name }}</option></select></label>
          <label>人工信息<select v-model.number="taskForm.identity_id"><option v-for="item in identities" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
          <label>支付方式<select v-model="taskForm.payment_method"><option value="credit_card">信用卡</option><option value="gift_card">礼品卡</option></select></label>
        </div>

        <div class="modal-actions">
          <button @click="closeModal">取消</button>
          <button class="primary" @click="submitModal">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, onBeforeUnmount, onMounted, reactive, ref } from "vue";

const API = "http://127.0.0.1:8020";
const DEFAULT_CDP = "http://127.0.0.1:9223";

const navItems = [
  { key: "dashboard", label: "工作台", desc: "查看业务数据概览与最近任务。" },
  { key: "skus", label: "SKU 管理", desc: "维护商品规格、选项与库存识别信息。" },
  { key: "identities", label: "人工信息", desc: "维护收货、联系人和支付信息套件。" },
  { key: "tasks", label: "下单任务", desc: "创建、编辑、执行任务并查看进度。" },
  { key: "prices", label: "SKU 价格", desc: "查看不同配置的价格采集结果。" },
];

const defaultOptionConfig = () => JSON.stringify({
  models: [
    { label: "iPhone 17 Pro Max", value: "6.9-inch-display" },
    { label: "iPhone 17 Pro", value: "6.3-inch-display" },
  ],
  storages: ["256gb", "512gb", "1tb", "2tb"],
  colors: ["silver", "cosmic-orange", "deep-blue"],
  carriers: [
    { label: "Connect later", value: "unlocked" },
    { label: "AT&T", value: "att" },
    { label: "T-Mobile", value: "t-mobile" },
    { label: "Verizon", value: "verizon" },
    { label: "Boost Mobile", value: "boost-mobile" },
  ],
}, null, 2);

const emptySku = () => ({ name: "", model_path: "6.9-inch-display", storage: "256gb", color: "silver", carrier: "unlocked", part_number: "", applecare: "none", payment_option: "fullPrice", option_config: defaultOptionConfig(), enabled: true });
const emptyIdentity = () => ({ name: "", first_name: "", last_name: "", email: "", phone: "", street: "", street2: "", city: "", state: "", zip_code: "", card_number: "", exp_month: "", exp_year: "", cvv: "", enabled: true });
const emptyTask = () => ({ name: "Apple 下单任务", sku_id: null, identity_id: null, cdp_url: DEFAULT_CDP, payment_method: "credit_card" });

const page = ref("dashboard");
const skus = ref([]);
const identities = ref([]);
const tasks = ref([]);
const priceRecords = ref([]);
const selectedTask = ref(null);
const taskDetail = ref({ logs: [] });
const healthStatus = ref("checking");
const modal = reactive({ open: false, type: "", mode: "create", id: null });
const skuForm = reactive(emptySku());
const identityForm = reactive(emptyIdentity());
const taskForm = reactive(emptyTask());
let timer;

const currentPage = computed(() => navItems.find((x) => x.key === page.value) || navItems[0]);
const healthText = computed(() => (healthStatus.value === "ok" ? "服务正常" : healthStatus.value === "error" ? "服务异常" : "检查中"));
const runningCount = computed(() => tasks.value.filter((x) => x.status === "running").length);
const modalTitle = computed(() => {
  const name = modal.type === "sku" ? "SKU" : modal.type === "identity" ? "人工信息" : "下单任务";
  return `${modal.mode === "edit" ? "编辑" : "新建"}${name}`;
});

const TaskTable = defineComponent({
  props: { tasks: { type: Array, default: () => [] } },
  emits: ["edit", "delete", "run", "select"],
  setup(props, { emit }) {
    return () => h("div", { class: "table-wrap" }, [
      h("table", [
        h("thead", [h("tr", [
          h("th", { class: "id-col" }, "ID"),
          h("th", "任务名称"),
          h("th", "SKU"),
          h("th", "人工信息"),
          h("th", "支付方式"),
          h("th", "状态"),
          h("th", "当前阶段"),
          h("th", "创建时间"),
          h("th", { class: "op-col" }, "操作"),
        ])]),
        h("tbody", props.tasks.length ? props.tasks.map((task) => h("tr", { onClick: () => emit("select", task) }, [
          h("td", task.id),
          h("td", { class: "strong" }, task.name),
          h("td", task.sku_name || "-"),
          h("td", task.identity_name || "-"),
          h("td", paymentLabel(task.payment_method)),
          h("td", [h("span", { class: ["status-pill", statusClass(task.status)] }, statusLabel(task.status))]),
          h("td", stageLabel(task.current_stage)),
          h("td", task.created_at || "-"),
          h("td", { class: "ops", onClick: (e) => e.stopPropagation() }, [
            h("button", { disabled: task.status === "running", onClick: () => emit("run", task) }, "执行"),
            h("button", { disabled: task.status === "running", onClick: () => emit("edit", task) }, "编辑"),
            h("button", { class: "danger", disabled: task.status === "running", onClick: () => emit("delete", task) }, "删除"),
          ]),
        ])) : [h("tr", [h("td", { class: "empty", colspan: 9 }, "暂无数据")])]),
      ]),
    ]);
  },
});

function assign(target, value) {
  Object.keys(target).forEach((key) => delete target[key]);
  Object.assign(target, value);
}

async function api(path, options) {
  const resp = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `HTTP ${resp.status}`);
  return resp.json();
}

async function checkHealth() {
  try {
    await api("/api/health");
    healthStatus.value = "ok";
  } catch {
    healthStatus.value = "error";
  }
}

async function loadAll() {
  await checkHealth();
  [skus.value, identities.value, tasks.value] = await Promise.all([api("/api/skus"), api("/api/identities"), api("/api/tasks")]);
  if (!taskForm.sku_id && skus.value[0]) taskForm.sku_id = skus.value[0].id;
  if (!taskForm.identity_id && identities.value[0]) taskForm.identity_id = identities.value[0].id;
  if (selectedTask.value) await loadTask(selectedTask.value.id);
}

async function loadPrices() {
  priceRecords.value = await api("/api/sku-price-records?limit=500");
}

function openSkuModal(item = null) {
  assign(skuForm, item ? { ...emptySku(), ...item, option_config: item.option_config || defaultOptionConfig(), enabled: Boolean(item.enabled) } : emptySku());
  Object.assign(modal, { open: true, type: "sku", mode: item ? "edit" : "create", id: item?.id || null });
}

function openIdentityModal(item = null) {
  const draft = item ? { ...emptyIdentity(), ...item, enabled: Boolean(item.enabled), card_number: "", cvv: "" } : emptyIdentity();
  assign(identityForm, draft);
  Object.assign(modal, { open: true, type: "identity", mode: item ? "edit" : "create", id: item?.id || null });
}

function openTaskModal(item = null) {
  const draft = item ? { ...emptyTask(), ...item } : { ...emptyTask(), sku_id: skus.value[0]?.id || null, identity_id: identities.value[0]?.id || null };
  assign(taskForm, draft);
  Object.assign(modal, { open: true, type: "task", mode: item ? "edit" : "create", id: item?.id || null });
}

function closeModal() {
  Object.assign(modal, { open: false, type: "", mode: "create", id: null });
}

async function submitModal() {
  if (modal.type === "sku") {
    const path = modal.mode === "edit" ? `/api/skus/${modal.id}` : "/api/skus";
    await api(path, { method: modal.mode === "edit" ? "PUT" : "POST", body: JSON.stringify(skuForm) });
  } else if (modal.type === "identity") {
    const path = modal.mode === "edit" ? `/api/identities/${modal.id}` : "/api/identities";
    await api(path, { method: modal.mode === "edit" ? "PUT" : "POST", body: JSON.stringify(identityForm) });
  } else if (modal.type === "task") {
    taskForm.cdp_url = taskForm.cdp_url || DEFAULT_CDP;
    const path = modal.mode === "edit" ? `/api/tasks/${modal.id}` : "/api/tasks";
    await api(path, { method: modal.mode === "edit" ? "PUT" : "POST", body: JSON.stringify(taskForm) });
  }
  closeModal();
  await loadAll();
}

async function deleteSku(item) {
  if (!confirm(`确认删除 SKU：${item.name}？`)) return;
  await api(`/api/skus/${item.id}`, { method: "DELETE" });
  await loadAll();
}

async function deleteIdentity(item) {
  if (!confirm(`确认删除信息：${item.name}？`)) return;
  await api(`/api/identities/${item.id}`, { method: "DELETE" });
  await loadAll();
}

async function deleteTask(item) {
  if (!confirm(`确认删除任务：${item.name}？`)) return;
  await api(`/api/tasks/${item.id}`, { method: "DELETE" });
  await loadAll();
}

async function selectTask(task) {
  selectedTask.value = task;
  page.value = "tasks";
  await loadTask(task.id);
}

async function loadTask(id) {
  taskDetail.value = await api(`/api/tasks/${id}`);
  selectedTask.value = { ...selectedTask.value, ...taskDetail.value };
}

async function runTask(task) {
  if (!task) return;
  await api(`/api/tasks/${task.id}/run`, { method: "POST" });
  selectedTask.value = task;
  page.value = "tasks";
  await loadAll();
}

function modelLabel(value) {
  return value === "6.9-inch-display" ? "iPhone 17 Pro Max" : value === "6.3-inch-display" ? "iPhone 17 Pro" : value === "homepod-mini" ? "HomePod mini" : value;
}

function carrierLabel(value) {
  const map = { unlocked: "Connect later", att: "AT&T", "t-mobile": "T-Mobile", verizon: "Verizon", "boost-mobile": "Boost Mobile", "": "-" };
  return map[value] || value;
}

function paymentLabel(value) {
  return value === "gift_card" ? "礼品卡" : "信用卡";
}

function statusClass(value) {
  return { running: "running", ready_for_submit: "done", failed: "failed", blocked: "blocked", created: "muted" }[value] || "muted";
}

function statusLabel(value) {
  return { created: "待执行", running: "执行中", ready_for_submit: "待确认", blocked: "需处理", failed: "失败" }[value] || value || "-";
}

function stageLabel(value) {
  return {
    created: "已创建",
    queued: "排队中",
    connect_browser: "连接浏览器",
    open_product: "打开商品",
    select_applecare: "选择服务",
    add_to_bag: "加入购物袋",
    bag: "购物袋",
    checkout: "结账",
    fulfillment: "配送信息",
    shipping: "收货信息",
    payment: "支付信息",
    review: "确认订单",
    ready_for_submit: "待最终确认",
    blocked: "等待处理",
    failed: "执行失败",
  }[value] || value || "-";
}

onMounted(async () => {
  await loadAll();
  await loadPrices();
  timer = setInterval(loadAll, 2000);
});

onBeforeUnmount(() => clearInterval(timer));
</script>
