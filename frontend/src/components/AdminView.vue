<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api";

const emit = defineEmits(["notify"]);

const loading = ref(true);
const activeTab = ref("overview");
const overview = ref(null);
const runs = ref([]);
const auditLogs = ref([]);
const curves = ref(null);
const curveLoading = ref(false);
const runLoading = ref(false);
const selectedRun = ref(null);
const selectedPointIndex = ref(0);
const curvePointIndex = ref(0);
const curveMetric = ref("response");
const curveForm = reactive({
  family: "f_strategy",
  stress: 65,
  energy: 55,
  baseline: 50,
  userId: ""
});

const tabs = [
  { key: "overview", label: "运营概览" },
  { key: "curves", label: "函数实验室" },
  { key: "runs", label: "运行剖析" },
  { key: "users", label: "用户与审计" }
];
const familyOptions = [
  { value: "f_strategy", label: "压力响应函数" },
  { value: "C_strategy", label: "连续负荷惩罚" },
  { value: "rest_strategy", label: "日间休息恢复" },
  { value: "night_strategy", label: "夜间恢复" }
];
const palette = ["#245d52", "#bd7b52", "#6d7fa8", "#8f6b91"];

const appData = computed(() => overview.value?.application || {});
const appCounts = computed(() => appData.value.counts || {});
const reliability = computed(() => overview.value?.reliability || {});
const latestEvaluation = computed(() => reliability.value.latest_evaluation || null);
const users = computed(() => appData.value.users || []);
const curveMetrics = computed(() => curves.value?.metrics || []);
const curvePointMax = computed(() =>
  Math.max(0, (curves.value?.series?.[0]?.points?.length || 1) - 1)
);
const curvePointRows = computed(() =>
  (curves.value?.series || []).map((series) => ({
    id: series.id,
    label: series.label,
    point: series.points?.[
      Math.min(Number(curvePointIndex.value), Math.max(0, (series.points?.length || 1) - 1))
    ] || null
  }))
);
const selectedPoint = computed(() => {
  const points = selectedRun.value?.points || [];
  return points[Math.min(Number(selectedPointIndex.value), Math.max(0, points.length - 1))] || null;
});
const selectedRunProfiles = computed(
  () => selectedRun.value?.diagnostics?.event_profiles || []
);

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function formatPercent(value) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

function evidenceLabel(level) {
  return {
    sufficient: "样本证据较充分",
    limited: "样本证据有限",
    insufficient: "样本不足"
  }[level] || "尚未评估";
}

function makePath(points, metric, width = 720, height = 280, fixedRange = null) {
  if (!points?.length) return "";
  const xValues = points.map((point, index) => Number(point.x ?? index));
  const yValues = points.map((point) => Number(point[metric] ?? 0));
  const minX = Math.min(...xValues);
  const maxX = Math.max(...xValues);
  let minY = fixedRange ? fixedRange[0] : Math.min(...yValues);
  let maxY = fixedRange ? fixedRange[1] : Math.max(...yValues);
  if (Math.abs(maxY - minY) < 0.000001) {
    minY -= 1;
    maxY += 1;
  } else if (!fixedRange) {
    const margin = (maxY - minY) * 0.12;
    minY -= margin;
    maxY += margin;
  }
  const padX = 44;
  const padY = 24;
  return points.map((point, index) => {
    const rawX = Number(point.x ?? index);
    const rawY = Number(point[metric] ?? 0);
    const x = padX + ((rawX - minX) / Math.max(1e-9, maxX - minX)) * (width - padX * 2);
    const y = height - padY - ((rawY - minY) / (maxY - minY)) * (height - padY * 2);
    return `${index ? "L" : "M"} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

const functionPaths = computed(() =>
  (curves.value?.series || []).map((series, index) => ({
    ...series,
    color: palette[index % palette.length],
    path: makePath(series.points, curveMetric.value)
  }))
);

const runPaths = computed(() => {
  const points = (selectedRun.value?.points || []).map((point, index) => ({
    ...point,
    x: index
  }));
  return [
    { label: "压力", color: "#a45c4a", path: makePath(points, "S", 720, 280, [0, 100]) },
    { label: "精力", color: "#245d52", path: makePath(points, "E", 720, 280, [0, 100]) }
  ];
});

async function loadOverview() {
  overview.value = await api("/api/admin/overview");
}

async function loadRuns() {
  const result = await api("/api/admin/prediction-runs?limit=40");
  runs.value = result.runs || [];
}

async function loadAudit() {
  const result = await api("/api/admin/audit-logs?limit=50");
  auditLogs.value = result.audit_logs || [];
}

async function loadCurve() {
  curveLoading.value = true;
  try {
    const query = new URLSearchParams({
      family: curveForm.family,
      stress: String(curveForm.stress),
      energy: String(curveForm.energy),
      baseline: String(curveForm.baseline)
    });
    if (curveForm.userId) query.set("user_id", curveForm.userId);
    const result = await api(`/api/admin/model/curves?${query.toString()}`);
    curves.value = result.curves;
    const allowedMetrics = (result.curves.metrics || []).map((metric) => metric.key);
    if (!allowedMetrics.includes(curveMetric.value)) {
      curveMetric.value = allowedMetrics[0] || "";
    }
    curvePointIndex.value = Math.floor(
      Math.max(0, (result.curves.series?.[0]?.points?.length || 1) - 1) / 2
    );
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    curveLoading.value = false;
  }
}

async function inspectRun(runId) {
  runLoading.value = true;
  selectedPointIndex.value = 0;
  try {
    const result = await api(`/api/admin/prediction-runs/${encodeURIComponent(runId)}`);
    selectedRun.value = result.run;
    const points = result.run.points || [];
    selectedPointIndex.value = Math.max(0, points.length - 1);
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    runLoading.value = false;
  }
}

async function toggleUser(item) {
  const nextState = !item.is_active;
  const action = nextState ? "启用" : "停用";
  if (!window.confirm(`确定要${action}账号“${item.login_id}”吗？`)) return;
  try {
    await api(`/api/admin/users/${item.id}/active`, {
      method: "PATCH",
      body: JSON.stringify({ is_active: nextState })
    });
    await loadOverview();
    emit("notify", `账号已${action}。`);
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  }
}

async function refreshAll() {
  loading.value = true;
  try {
    await Promise.all([loadOverview(), loadRuns(), loadAudit()]);
    await loadCurve();
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    loading.value = false;
  }
}

onMounted(refreshAll);
</script>

<template>
  <section class="admin-workspace">
    <div class="page-intro admin-intro">
      <div>
        <p class="eyebrow">MANAGEMENT CONSOLE</p>
        <h2>管理与模型诊断</h2>
        <p>集中查看用户覆盖、模型运行、函数分解和反馈证据。所有可靠性指标都附带样本量。</p>
      </div>
      <button class="outline-button" type="button" :disabled="loading" @click="refreshAll">
        {{ loading ? "更新中…" : "刷新数据" }}
      </button>
    </div>

    <nav class="admin-tabs" aria-label="管理后台视图">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div v-if="loading && !overview" class="surface-card admin-loading">
      正在汇总管理数据…
    </div>

    <template v-else>
      <div v-show="activeTab === 'overview'" class="admin-panel-stack">
        <div class="admin-metric-grid">
          <article>
            <span>账号总数</span>
            <strong>{{ appCounts.users || 0 }}</strong>
            <small>{{ appCounts.active_users || 0 }} 个启用</small>
          </article>
          <article>
            <span>画像覆盖</span>
            <strong>{{ formatPercent(appData.profile_coverage || 0) }}</strong>
            <small>{{ appCounts.profiles || 0 }} 位已建立画像</small>
          </article>
          <article>
            <span>预测运行</span>
            <strong>{{ appCounts.prediction_runs || 0 }}</strong>
            <small>{{ appCounts.diagnostic_runs || 0 }} 次含公式诊断</small>
          </article>
          <article>
            <span>反馈记录</span>
            <strong>{{ appCounts.feedback || 0 }}</strong>
            <small>每次运行 {{ formatNumber(appData.feedback_per_run || 0, 2) }} 条</small>
          </article>
        </div>

        <div class="admin-two-column">
          <article class="surface-card evidence-card">
            <div class="card-heading">
              <div><p class="eyebrow">RELIABILITY EVIDENCE</p><h3>可靠性证据</h3></div>
              <span class="status-pill" :class="reliability.evidence_level === 'sufficient' ? 'safe' : 'warning'">
                {{ evidenceLabel(reliability.evidence_level) }}
              </span>
            </div>
            <div v-if="latestEvaluation" class="evidence-metrics">
              <div><span>评估样本</span><strong>{{ latestEvaluation.sample_count }}</strong></div>
              <div><span>压力 MAE</span><strong>{{ formatNumber(latestEvaluation.stress_mae) }}</strong></div>
              <div><span>精力 MAE</span><strong>{{ formatNumber(latestEvaluation.energy_mae) }}</strong></div>
              <div><span>趋势命中</span><strong>{{ formatPercent(latestEvaluation.trend_accuracy) }}</strong></div>
              <div><span>峰值时间误差</span><strong>{{ formatNumber(latestEvaluation.peak_time_error_min, 0) }} 分</strong></div>
              <div><span>综合损失</span><strong>{{ formatNumber(latestEvaluation.total_loss) }}</strong></div>
            </div>
            <div v-else class="evidence-empty">
              <strong>尚无已保存的误差评估</strong>
              <p>先通过反馈形成观测锚点，再运行评估；后台不会在没有样本时生成“置信度”。</p>
            </div>
            <p class="evidence-note">{{ reliability.evidence_note }}</p>
          </article>

          <article class="surface-card evidence-card">
            <div class="card-heading">
              <div><p class="eyebrow">DATA READINESS</p><h3>数据就绪度</h3></div>
            </div>
            <div class="readiness-list">
              <div>
                <span>日级反馈</span><strong>{{ reliability.counts?.daily_feedback || 0 }}</strong>
              </div>
              <div>
                <span>事件纠错</span><strong>{{ reliability.counts?.event_feedback || 0 }}</strong>
              </div>
              <div>
                <span>误差评估</span><strong>{{ reliability.counts?.evaluations || 0 }}</strong>
              </div>
              <div>
                <span>校准任务</span><strong>{{ reliability.counts?.calibration_jobs || 0 }}</strong>
              </div>
            </div>
            <div class="admin-version-strip">
              <span>模型 {{ overview?.versions?.model }}</span>
              <span>参数 {{ overview?.versions?.parameters }}</span>
              <span>特征 {{ overview?.versions?.features }}</span>
            </div>
          </article>
        </div>
      </div>

      <div v-show="activeTab === 'curves'" class="admin-panel-stack">
        <article class="surface-card curve-controls">
          <div class="curve-control-grid">
            <label>
              <span>函数族</span>
              <select v-model="curveForm.family" class="clean-input" @change="loadCurve">
                <option v-for="item in familyOptions" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
            </label>
            <label>
              <span>参数来源</span>
              <select v-model="curveForm.userId" class="clean-input" @change="loadCurve">
                <option value="">当前管理员参数</option>
                <option v-for="item in users" :key="item.id" :value="String(item.id)">
                  {{ item.login_id }}
                </option>
              </select>
            </label>
            <label>
              <span>压力 {{ curveForm.stress }}</span>
              <input v-model.number="curveForm.stress" type="range" min="0" max="100" @change="loadCurve">
            </label>
            <label>
              <span>精力 {{ curveForm.energy }}</span>
              <input v-model.number="curveForm.energy" type="range" min="0" max="100" @change="loadCurve">
            </label>
            <label>
              <span>基线 S* {{ curveForm.baseline }}</span>
              <input v-model.number="curveForm.baseline" type="range" min="0" max="100" @change="loadCurve">
            </label>
          </div>
        </article>

        <article v-if="curves" class="surface-card function-chart-card">
          <div class="card-heading">
            <div>
              <p class="eyebrow">FUNCTION EXPLORER</p>
              <h3>{{ curves.label }}</h3>
              <p>{{ curves.description }}</p>
            </div>
            <select v-model="curveMetric" class="clean-input metric-select">
              <option v-for="metric in curveMetrics" :key="metric.key" :value="metric.key">
                {{ metric.label }}
              </option>
            </select>
          </div>
          <div class="diagnostic-chart" :class="{ loading: curveLoading }">
            <svg viewBox="0 0 720 280" role="img" :aria-label="`${curves.label}函数对比图`">
              <line x1="44" y1="256" x2="676" y2="256" />
              <line x1="44" y1="24" x2="44" y2="256" />
              <path
                v-for="item in functionPaths"
                :key="item.id"
                :d="item.path"
                :stroke="item.color"
              />
            </svg>
          </div>
          <div class="chart-legend">
            <span v-for="item in functionPaths" :key="item.id">
              <i :style="{ background: item.color }"></i>{{ item.label }}
            </span>
          </div>
          <div class="curve-point-inspector">
            <label>
              <span>
                精确取值 · {{ curves.x_axis.label }}
                {{ curvePointRows[0]?.point?.x ?? "—" }} {{ curves.x_axis.unit }}
              </span>
              <input
                v-model.number="curvePointIndex"
                type="range"
                min="0"
                :max="curvePointMax"
              >
            </label>
            <div>
              <span v-for="item in curvePointRows" :key="item.id">
                <b>{{ item.label }}</b>
                {{ formatNumber(item.point?.[curveMetric], 4) }}
              </span>
            </div>
          </div>
          <div class="curve-notes">
            <span>横轴：{{ curves.x_axis.label }}（{{ curves.x_axis.unit }}）</span>
            <span v-for="note in curves.assumptions" :key="note">{{ note }}</span>
          </div>
          <div class="formula-grid">
            <details v-for="item in functionPaths" :key="item.id">
              <summary>{{ item.label }} · 计算说明</summary>
              <p>{{ item.summary }}</p>
              <code v-if="item.trace">{{ item.trace }}</code>
              <small v-else>该策略当前没有额外公式日志，曲线仍由真实实现逐点计算。</small>
            </details>
          </div>
        </article>
      </div>

      <div v-show="activeTab === 'runs'" class="admin-run-layout">
        <article class="surface-card run-list-card">
          <div class="card-heading">
            <div><p class="eyebrow">REPLAYABLE RUNS</p><h3>已保存运行</h3></div>
            <span>{{ runs.length }} 条</span>
          </div>
          <button
            v-for="run in runs"
            :key="run.prediction_run_id"
            class="admin-run-row"
            :class="{ active: selectedRun?.prediction_run_id === run.prediction_run_id }"
            type="button"
            @click="inspectRun(run.prediction_run_id)"
          >
            <span><b>{{ run.local_date }}</b><small>{{ run.login_id }}</small></span>
            <span>压力 {{ formatNumber(run.result?.end_S, 0) }}</span>
            <span>精力 {{ formatNumber(run.result?.end_E, 0) }}</span>
            <em :class="{ ready: run.has_diagnostics }">{{ run.has_diagnostics ? "可分解" : "仅轨迹" }}</em>
          </button>
          <div v-if="!runs.length" class="evidence-empty">尚无预测运行。</div>
        </article>

        <article class="surface-card run-inspector-card">
          <div v-if="runLoading" class="admin-loading">正在加载完整轨迹…</div>
          <div v-else-if="!selectedRun" class="run-placeholder">
            <span>⌁</span>
            <h3>选择一次运行查看计算剖面</h3>
            <p>这里会显示逐点曲线、疲劳惩罚、基础增量、事件贡献和公式记录。</p>
          </div>
          <template v-else>
            <div class="card-heading">
              <div>
                <p class="eyebrow">RUN INSPECTOR</p>
                <h3>{{ selectedRun.local_date }} · {{ selectedRun.login_id }}</h3>
                <p>种子 {{ selectedRun.random_seed }} · {{ selectedRun.points.length }} 个状态点</p>
              </div>
              <code class="fingerprint">{{ selectedRun.result?.fingerprint?.slice(0, 12) }}…</code>
            </div>
            <div class="diagnostic-chart compact">
              <svg viewBox="0 0 720 280" role="img" aria-label="运行压力和精力曲线">
                <line x1="44" y1="256" x2="676" y2="256" />
                <line x1="44" y1="24" x2="44" y2="256" />
                <path v-for="item in runPaths" :key="item.label" :d="item.path" :stroke="item.color" />
              </svg>
            </div>
            <div class="chart-legend">
              <span v-for="item in runPaths" :key="item.label">
                <i :style="{ background: item.color }"></i>{{ item.label }}
              </span>
            </div>

            <div v-if="selectedPoint" class="point-inspector">
              <label>
                <span>检查时间点 {{ selectedPoint.time }}</span>
                <input
                  v-model.number="selectedPointIndex"
                  type="range"
                  min="0"
                  :max="Math.max(0, selectedRun.points.length - 1)"
                >
              </label>
              <div class="point-metrics">
                <div><span>压力 S</span><strong>{{ formatNumber(selectedPoint.S) }}</strong></div>
                <div><span>精力 E</span><strong>{{ formatNumber(selectedPoint.E) }}</strong></div>
                <div><span>本步 ΔS</span><strong>{{ formatNumber(selectedPoint.delta_S, 4) }}</strong></div>
                <div><span>基础 ΔS</span><strong>{{ formatNumber(Number(selectedPoint.delta_S || 0) - Number(selectedPoint.f_pen || 0), 4) }}</strong></div>
                <div><span>疲劳惩罚</span><strong>{{ formatNumber(selectedPoint.f_pen, 4) }}</strong></div>
                <div><span>连续负荷</span><strong>{{ formatNumber(selectedPoint.continuous_hours) }}h</strong></div>
              </div>
              <div class="point-context">
                <span>状态：{{ selectedPoint.state }}</span>
                <span>当前事件：{{ selectedPoint.current_events?.join("、") || "无" }}</span>
                <span>主导压力源：{{ selectedPoint.dominant_stressors?.join("、") || "无" }}</span>
              </div>
            </div>

            <div class="event-profile-table">
              <div class="card-heading">
                <div><p class="eyebrow">EVENT CONTRIBUTIONS</p><h3>事件级贡献分解</h3></div>
                <span>{{ selectedRunProfiles.length }} 个事件</span>
              </div>
              <div v-if="selectedRunProfiles.length" class="admin-table-wrap">
                <table>
                  <thead><tr><th>事件</th><th>总压力</th><th>基础压力</th><th>疲劳惩罚</th><th>精力影响</th><th>公式</th></tr></thead>
                  <tbody>
                    <tr v-for="item in selectedRunProfiles" :key="`${item.name}-${item.time}`">
                      <td><b>{{ item.name }}</b><small>{{ item.time }}</small></td>
                      <td>{{ formatNumber(item.s_impact) }}</td>
                      <td>{{ formatNumber(item.base_s) }}</td>
                      <td>{{ formatNumber(item.penalty_s) }}</td>
                      <td>{{ formatNumber(item.e_impact) }}</td>
                      <td><details><summary>查看</summary><code>{{ item.math_trace || "未记录" }}</code></details></td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="evidence-empty">
                这次运行创建于诊断存储启用之前；逐点轨迹仍可查看，新运行会保存公式分解。
              </div>
            </div>
          </template>
        </article>
      </div>

      <div v-show="activeTab === 'users'" class="admin-two-column users-audit">
        <article class="surface-card">
          <div class="card-heading">
            <div><p class="eyebrow">USER VIEWS</p><h3>用户数据视图</h3></div>
            <span>{{ users.length }} 位</span>
          </div>
          <div class="admin-table-wrap">
            <table>
              <thead><tr><th>用户</th><th>角色</th><th>画像</th><th>运行</th><th>反馈</th><th>状态</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="item in users" :key="item.id">
                  <td><b>{{ item.login_id }}</b><small>#{{ item.id }}</small></td>
                  <td>{{ item.role }}</td>
                  <td>{{ item.has_profile ? "已建立" : "未建立" }}</td>
                  <td>{{ item.run_count }}</td>
                  <td>{{ item.feedback_count }}</td>
                  <td><span class="status-pill" :class="item.is_active ? 'safe' : 'warning'">{{ item.is_active ? "启用" : "停用" }}</span></td>
                  <td><button class="table-action" type="button" @click="toggleUser(item)">{{ item.is_active ? "停用" : "启用" }}</button></td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
        <article class="surface-card">
          <div class="card-heading">
            <div><p class="eyebrow">AUDIT TRAIL</p><h3>最近审计记录</h3></div>
            <span>{{ auditLogs.length }} 条</span>
          </div>
          <div class="audit-list">
            <div v-for="item in auditLogs" :key="item.id">
              <span><b>{{ item.action }}</b><small>{{ item.login_id || "系统" }}</small></span>
              <time>{{ item.created_at }}</time>
              <details><summary>详情</summary><code>{{ JSON.stringify(item.details, null, 2) }}</code></details>
            </div>
          </div>
        </article>
      </div>
    </template>
  </section>
</template>
