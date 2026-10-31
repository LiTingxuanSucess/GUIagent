<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand-block">
        <div class="brand-mark">EC</div>
        <div>
          <h1>价格追踪</h1>
          <span>Agent Console</span>
        </div>
      </div>
      <nav>
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
          <button @click="loadAll">刷新</button>
          <button v-if="page === 'tasks'" class="primary" :disabled="running" @click="startRun">
            {{ running ? "采集中..." : "立即采集" }}
          </button>
        </div>
      </header>

      <section v-if="page === 'dashboard'" class="page-stack">
        <div class="metrics">
          <div class="metric"><span>品牌</span><strong>{{ brands.length }}</strong></div>
          <div class="metric"><span>系列</span><strong>{{ seriesList.length }}</strong></div>
          <div class="metric"><span>平台</span><strong>{{ platforms.length }}</strong></div>
          <div class="metric"><span>价格记录</span><strong>{{ products.length }}</strong></div>
          <div class="metric"><span>最新批次</span><strong>{{ latestRunLabel }}</strong></div>
        </div>
        <div class="panel chart-panel large">
          <div class="panel-head">
            <div>
              <h3>价格走势</h3>
              <p>按批次时间点展示 SKU/Item 最低可见价。</p>
            </div>
            <select v-model="filters.platform" @change="reloadFiltered">
              <option value="">全部平台</option>
              <option v-for="platform in platforms" :key="platform.code" :value="platform.code">{{ platform.name }}</option>
            </select>
          </div>
          <div ref="chartRef" class="echart echart-large"></div>
        </div>
        <div class="panel">
          <div class="panel-head compact">
            <h3>最新价格记录</h3>
            <button @click="page = 'records'">查看全部</button>
          </div>
          <RecordTable :items="filteredProducts.slice(0, 8)" :platform-name="platformName" :asset-url="assetUrl" />
        </div>
      </section>

      <section v-else-if="page === 'tasks'" class="page-stack">
        <div class="panel capture-panel">
          <div class="panel-head">
            <div>
              <h3>采集配置</h3>
              <p>每次执行生成一个批次，抓取结果直接写入 SQLite。</p>
            </div>
          </div>
          <div class="form-grid">
            <label>
              品牌
              <select v-model.number="runForm.brand_id" @change="syncKeyword">
                <option v-for="brand in brands" :key="brand.id" :value="brand.id">{{ brand.name }}</option>
              </select>
            </label>
            <label>
              系列
              <select v-model.number="runForm.series_id" @change="syncKeyword">
                <option v-for="item in seriesList" :key="item.id" :value="item.id">{{ item.name }}</option>
              </select>
            </label>
            <label>
              采集数量
              <input v-model.number="runForm.limit" type="number" min="1" max="30" />
            </label>
            <label class="wide">
              搜索关键词
              <input v-model="runForm.keyword" />
            </label>
          </div>
          <div class="platform-pills">
            <label v-for="platform in platforms" :key="platform.code" :class="{ checked: runForm.platforms.includes(platform.code) }">
              <input v-model="runForm.platforms" type="checkbox" :value="platform.code" />
              {{ platform.name }}
            </label>
          </div>
        </div>
        <div class="panel runs-panel">
          <div class="panel-head compact">
            <h3>采集批次</h3>
            <span>{{ runs.length }} 条</span>
          </div>
          <div class="run-list vertical">
            <button v-for="run in runs" :key="run.id" :class="{ selected: filters.run_id === run.id }" @click="selectRun(run)">
              <strong>#{{ run.id }}</strong>
              <span>{{ run.brand }} {{ run.series }}</span>
              <em>{{ run.platforms }}</em>
              <i :class="run.status">{{ run.status }}</i>
            </button>
          </div>
        </div>
      </section>

      <section v-else-if="page === 'catalog'" class="config-grid">
        <EntityPanel title="品牌管理" :items="brands" v-model:name="brandDraft.name" v-model:sub="brandDraft.aliases" sub-placeholder="别名" @save="saveBrand" />
        <EntityPanel title="系列管理" :items="seriesList" v-model:name="seriesDraft.name" v-model:sub="seriesDraft.keywords" sub-placeholder="关键词" @save="saveSeries" />
      </section>

      <section v-else-if="page === 'platforms'" class="config-grid single">
        <EntityPanel title="平台管理" :items="platforms" label-field="name" sub-field="code" v-model:name="platformDraft.name" v-model:sub="platformDraft.code" sub-placeholder="code" @save="savePlatform" />
      </section>

      <section v-else-if="page === 'schedule'" class="page-stack">
        <div class="panel schedule-panel">
          <div class="panel-head">
            <div>
              <h3>定时触发设置</h3>
              <p>当前为前端配置页，保存到本地浏览器。后续可接后端 APScheduler 或 Windows Task Scheduler。</p>
            </div>
            <button class="primary" @click="saveSchedule">保存设置</button>
          </div>
          <div class="schedule-grid">
            <label>
              启用定时
              <select v-model="schedule.enabled">
                <option :value="true">启用</option>
                <option :value="false">停用</option>
              </select>
            </label>
            <label>
              执行频率
              <select v-model="schedule.frequency">
                <option value="daily">每天</option>
                <option value="weekly">每周</option>
                <option value="interval">按间隔</option>
              </select>
            </label>
            <label>
              执行时间
              <input v-model="schedule.time" type="time" />
            </label>
            <label>
              间隔小时
              <input v-model.number="schedule.intervalHours" type="number" min="1" max="168" />
            </label>
            <label class="wide">
              默认平台
              <input v-model="schedule.platforms" placeholder="jd,tmall,taobao" />
            </label>
            <label class="wide">
              默认关键词
              <input v-model="schedule.keyword" placeholder="杜蕾斯 001" />
            </label>
          </div>
          <div class="schedule-preview">
            <strong>当前计划：</strong>
            <span>{{ scheduleText }}</span>
          </div>
        </div>
      </section>

      <section v-else-if="page === 'records'" class="panel table-panel">
        <div class="panel-head">
          <div>
            <h3>价格记录</h3>
            <p>共 {{ filteredProducts.length }} 条，当前显示 {{ pageStart }}-{{ pageEnd }} 条。</p>
          </div>
          <div class="table-tools">
            <select v-model="filters.platform" @change="reloadFiltered">
              <option value="">全部平台</option>
              <option v-for="platform in platforms" :key="platform.code" :value="platform.code">{{ platform.name }}</option>
            </select>
            <input v-model="search" placeholder="搜索商品或店铺" />
          </div>
        </div>
        <RecordTable :items="pagedProducts" :platform-name="platformName" :asset-url="assetUrl" />
        <div class="pagination-bar">
          <div class="pagination-summary">
            <strong>{{ filteredProducts.length }}</strong>
            <span>条记录</span>
          </div>
          <div class="pagination-controls">
            <label>
              每页
              <select v-model.number="pageSize">
                <option :value="10">10</option>
                <option :value="20">20</option>
                <option :value="50">50</option>
                <option :value="100">100</option>
              </select>
            </label>
            <button :disabled="recordPage <= 1" @click="recordPage = 1">首页</button>
            <button :disabled="recordPage <= 1" @click="recordPage -= 1">上一页</button>
            <span>第 {{ recordPage }} / {{ totalPages }} 页</span>
            <button :disabled="recordPage >= totalPages" @click="recordPage += 1">下一页</button>
            <button :disabled="recordPage >= totalPages" @click="recordPage = totalPages">末页</button>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import * as echarts from "echarts";
import { computed, defineComponent, h, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8010").replace(/\/$/, "");

const RecordTable = defineComponent({
  props: { items: Array, platformName: Function, assetUrl: Function },
  setup(props) {
    return () => h("div", { class: "table-wrap" }, [
      h("table", [
        h("thead", h("tr", ["时间", "平台", "店铺", "商品", "主品/组合", "到手价", "片数", "片单", "销量", "截图"].map((x) => h("th", { class: x === "商品" ? "product" : "" }, x)))),
        h("tbody", props.items.map((item) => h("tr", { key: item.id }, [
          h("td", fmt(item.captured_at)),
          h("td", h("span", { class: "platform-badge" }, props.platformName(item.platform))),
          h("td", item.store_label || item.shop_name || "-"),
          h("td", { class: "product" }, h("a", { href: item.product_url, target: "_blank" }, item.title)),
          h("td", item.product_combo || item.main_product_name || "-"),
          h("td", { class: "num strong" }, money(item.final_price)),
          h("td", { class: "num" }, item.piece_count || "-"),
          h("td", { class: "num" }, money(item.unit_price)),
          h("td", item.sales_text || "-"),
          h("td", item.source_page_screenshot_path ? h("a", { href: props.assetUrl(item.source_page_screenshot_path), target: "_blank" }, "查看") : ""),
        ]))),
      ]),
    ]);
  },
});

const EntityPanel = defineComponent({
  props: {
    title: String,
    items: Array,
    name: String,
    sub: String,
    subPlaceholder: String,
    labelField: { type: String, default: "name" },
    subField: { type: String, default: "aliases" },
  },
  emits: ["update:name", "update:sub", "save"],
  setup(props, { emit }) {
    return () => h("div", { class: "panel" }, [
      h("div", { class: "panel-head compact" }, [h("h3", props.title)]),
      h("div", { class: "inline-form" }, [
        h("input", { value: props.name, placeholder: "名称", onInput: (e) => emit("update:name", e.target.value) }),
        h("input", { value: props.sub, placeholder: props.subPlaceholder, onInput: (e) => emit("update:sub", e.target.value) }),
        h("button", { onClick: () => emit("save") }, "新增"),
      ]),
      h("div", { class: "tag-list" }, props.items.map((item) => h("span", { key: item.id }, props.subField && item[props.subField] ? `${item[props.labelField]} / ${item[props.subField]}` : item[props.labelField]))),
    ]);
  },
});

const navItems = [
  { key: "dashboard", label: "价格看板", desc: "查看核心指标、价格走势和最新记录。" },
  { key: "tasks", label: "采集任务", desc: "配置平台、关键词和采集批次。" },
  { key: "catalog", label: "品牌系列", desc: "维护品牌和系列基础数据。" },
  { key: "platforms", label: "平台配置", desc: "维护电商平台开关和展示名称。" },
  { key: "schedule", label: "定时设置", desc: "配置前端定时触发计划。" },
  { key: "records", label: "价格记录", desc: "查看所有入库价格快照。" },
];

const page = ref("dashboard");
const currentPage = computed(() => navItems.find((x) => x.key === page.value) || navItems[0]);
const brands = ref([]);
const seriesList = ref([]);
const platforms = ref([]);
const runs = ref([]);
const products = ref([]);
const trendItems = ref([]);
const running = ref(false);
const search = ref("");
const recordPage = ref(1);
const pageSize = ref(20);
const chartRef = ref(null);
let chart;

const filters = reactive({ platform: "", run_id: null });
const runForm = reactive({ brand_id: null, series_id: null, keyword: "杜蕾斯 001", platforms: ["jd"], limit: 8 });
const brandDraft = reactive({ name: "", aliases: "", enabled: true });
const seriesDraft = reactive({ name: "", keywords: "", exclude_keywords: "", enabled: true });
const platformDraft = reactive({ code: "", name: "", enabled: true });
const schedule = reactive(JSON.parse(localStorage.getItem("ecagent_schedule") || '{"enabled":false,"frequency":"daily","time":"09:00","intervalHours":24,"platforms":"jd,tmall,taobao","keyword":"杜蕾斯 001"}'));

async function api(path, options) {
  const resp = await fetch(`${API_BASE}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `HTTP ${resp.status}`);
  return resp.json();
}

async function loadAll() {
  const [b, s, p, r] = await Promise.all([api("/api/brands"), api("/api/series"), api("/api/platforms"), api("/api/runs")]);
  brands.value = b;
  seriesList.value = s;
  platforms.value = p;
  runs.value = r;
  if (!runForm.brand_id && b.length) runForm.brand_id = b[0].id;
  if (!runForm.series_id && s.length) runForm.series_id = s[0].id;
  syncKeyword();
  await Promise.all([loadProducts(), loadTrends()]);
}

function syncKeyword() {
  const brand = brands.value.find((x) => x.id === runForm.brand_id)?.name || "杜蕾斯";
  const series = seriesList.value.find((x) => x.id === runForm.series_id)?.name || "001";
  runForm.keyword = `${brand} ${series}`.trim();
}

async function startRun() {
  running.value = true;
  try {
    await api("/api/runs", { method: "POST", body: JSON.stringify(runForm) });
    await pollRuns();
  } finally {
    running.value = false;
  }
}

async function pollRuns() {
  for (let i = 0; i < 60; i++) {
    runs.value = await api("/api/runs");
    if (!runs.value.some((x) => x.status === "running")) break;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  await Promise.all([loadProducts(), loadTrends()]);
}

async function reloadFiltered() {
  await Promise.all([loadProducts(), loadTrends()]);
}

async function loadProducts() {
  const params = new URLSearchParams();
  if (filters.platform) params.set("platform", filters.platform);
  if (filters.run_id) params.set("run_id", filters.run_id);
  products.value = await api(`/api/products?${params.toString()}`);
}

async function loadTrends() {
  const params = new URLSearchParams();
  if (filters.platform) params.set("platform", filters.platform);
  trendItems.value = (await api(`/api/trends?${params.toString()}`)).items || [];
  await nextTick();
  renderChart();
}

function selectRun(run) {
  filters.run_id = filters.run_id === run.id ? null : run.id;
  loadProducts();
}

async function saveBrand() {
  if (!brandDraft.name.trim()) return;
  await api("/api/brands", { method: "POST", body: JSON.stringify(brandDraft) });
  brandDraft.name = "";
  brandDraft.aliases = "";
  await loadAll();
}

async function saveSeries() {
  if (!seriesDraft.name.trim()) return;
  await api("/api/series", { method: "POST", body: JSON.stringify(seriesDraft) });
  seriesDraft.name = "";
  seriesDraft.keywords = "";
  await loadAll();
}

async function savePlatform() {
  if (!platformDraft.code.trim() || !platformDraft.name.trim()) return;
  await api("/api/platforms", { method: "POST", body: JSON.stringify(platformDraft) });
  platformDraft.code = "";
  platformDraft.name = "";
  await loadAll();
}

function saveSchedule() {
  localStorage.setItem("ecagent_schedule", JSON.stringify(schedule));
}

function renderChart() {
  if (!chartRef.value || page.value !== "dashboard") return;
  if (!chart) chart = echarts.init(chartRef.value);
  const groups = new Map();
  for (const item of trendItems.value) {
    const key = `${platformName(item.platform)} · ${item.title || item.product_id}`.slice(0, 42);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push([item.point_time || item.day, Number(item.min_price)]);
  }
  const series = Array.from(groups.entries()).slice(0, 8).map(([name, data]) => ({
    name,
    type: "line",
    smooth: true,
    symbolSize: 7,
    data: data.sort((a, b) => String(a[0]).localeCompare(String(b[0]))),
  }));
  chart.setOption({
    color: ["#2563eb", "#16a34a", "#dc2626", "#7c3aed", "#ea580c", "#0891b2", "#be123c", "#4f46e5"],
    tooltip: { trigger: "axis" },
    legend: { type: "scroll", top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 44, right: 18, top: 46, bottom: 36 },
    xAxis: { type: "category", boundaryGap: false },
    yAxis: { type: "value", axisLabel: { formatter: "¥{value}" } },
    series,
  });
}

const filteredProducts = computed(() => {
  const q = search.value.trim().toLowerCase();
  if (!q) return products.value;
  return products.value.filter((item) => [item.title, item.shop_name, item.store_label].join(" ").toLowerCase().includes(q));
});
const totalPages = computed(() => Math.max(1, Math.ceil(filteredProducts.value.length / pageSize.value)));
const pageStart = computed(() => (filteredProducts.value.length ? (recordPage.value - 1) * pageSize.value + 1 : 0));
const pageEnd = computed(() => Math.min(recordPage.value * pageSize.value, filteredProducts.value.length));
const pagedProducts = computed(() => filteredProducts.value.slice(pageStart.value ? pageStart.value - 1 : 0, pageEnd.value));

const latestRunLabel = computed(() => (runs.value[0] ? `#${runs.value[0].id}` : "-"));
const scheduleText = computed(() => {
  if (!schedule.enabled) return "未启用";
  if (schedule.frequency === "interval") return `每 ${schedule.intervalHours} 小时执行一次，平台 ${schedule.platforms}，关键词 ${schedule.keyword}`;
  return `${schedule.frequency === "daily" ? "每天" : "每周"} ${schedule.time} 执行，平台 ${schedule.platforms}，关键词 ${schedule.keyword}`;
});

function platformName(code) {
  return platforms.value.find((x) => x.code === code)?.name || code;
}

function money(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  return Number.isFinite(n) ? `¥${n.toFixed(2)}` : "-";
}

function fmt(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").slice(0, 16);
}

function assetUrl(path) {
  const normalized = String(path || "").replaceAll("\\", "/");
  const marker = "/exports/";
  if (normalized.includes(marker)) return `${API_BASE}/exports/${normalized.split(marker)[1]}`;
  if (normalized.startsWith("exports/")) return `${API_BASE}/${normalized}`;
  return `${API_BASE}/exports/${normalized.split("exports/").pop()}`;
}

function onResize() {
  chart?.resize();
}

watch(page, async () => {
  await nextTick();
  renderChart();
  chart?.resize();
});

watch([search, pageSize, () => filters.platform, () => filters.run_id], () => {
  recordPage.value = 1;
});

watch(totalPages, (next) => {
  if (recordPage.value > next) recordPage.value = next;
});

onMounted(async () => {
  await loadAll();
  window.addEventListener("resize", onResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
  chart?.dispose();
});
</script>
