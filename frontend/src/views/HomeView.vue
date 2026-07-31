<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api, currentLocalDate } from "../api";
import OnboardingDialog from "../components/OnboardingDialog.vue";
import ApiKeyDialog from "../components/ApiKeyDialog.vue";
import StrategyPreferences from "../components/StrategyPreferences.vue";
import AdminView from "../components/AdminView.vue";

const router = useRouter();
const onboardingDialog = ref(null);
const apiKeyDialog = ref(null);
const dashboard = ref(null);
const activeView = ref("dashboard");
const sidebarOpen = ref(false);
const loading = ref(true);
const tokenStatus = ref({
  valid: false,
  connected: false,
  status: "missing",
  refreshable: false,
  needs_reauthorization: false
});
const feishuConnecting = ref(false);
const feishuChecking = ref(false);
const feishuVerification = ref(null);
const activeKeyCount = ref(0);
const toasts = ref([]);
const mockEvents = ref([]);
const predictionLoading = ref(false);
const prediction = ref(null);
const chartSource = ref("");
const feedbackLoading = ref(false);
const reviewLoading = ref(false);

const predictionForm = reactive({
  date: currentLocalDate(),
  initS: 50,
  initE: 80,
  eventName: "",
  eventType: "task",
  eventLevel: "general",
  eventStart: "14:00",
  eventEnd: "15:30"
});
const feedbackForm = reactive({
  period: "morning",
  stress: 5,
  energy: 6,
  note: ""
});
const reviewForm = reactive({
  type: "peak_review",
  time: new Date().toTimeString().slice(0, 5),
  score: 5,
  note: ""
});

const viewLabels = {
  dashboard: "今日概览",
  profile: "我的画像",
  prediction: "趋势预测",
  feedback: "轻量反馈",
  settings: "设置",
  admin: "管理与诊断"
};
const routineLabels = { lunch: "午餐", nap: "午间休息", dinner: "晚餐" };
const routineStatusLabels = {
  scheduled: "理想时间",
  shifted: "已避开日程",
  shortened: "已缩短",
  unavailable: "时间冲突",
  not_expected: "按你的习惯"
};
const reviewLabels = {
  peak_review: "实际峰值（0–10）",
  event_impact: "实际影响（0–10）",
  prediction_review: "预警准确度（0–10）",
  care_review: "帮助程度（0–10）",
  routine_correction: "与计划符合度（0–10）"
};

const user = computed(() => dashboard.value?.user || {});
const isAdmin = computed(() => user.value.role === "admin");
const userLabel = computed(() => {
  const loginId = user.value.login_id || "";
  if (user.value.login_type === "email") return loginId.split("@")[0] || "朋友";
  return loginId || "朋友";
});
const profile = computed(() => dashboard.value?.profile || null);
const routinePlan = computed(() => dashboard.value?.routine_plan || prediction.value?.routine_plan || null);
const recentRuns = computed(() => dashboard.value?.recent_runs || []);
const latestRun = computed(() => recentRuns.value[0] || null);
const latestResult = computed(() => latestRun.value?.result || null);
const versions = computed(() => dashboard.value?.versions || {});
const onboardingCompleted = computed(() => Boolean(dashboard.value?.onboarding_completed));
const tokenValid = computed(() => Boolean(tokenStatus.value.valid));
const feishuRedirectUri = computed(() => tokenStatus.value.redirect_uri || "");
const feishuOauthAppId = computed(() => tokenStatus.value.oauth_app_id || "");
const feishuStatusLabel = computed(() => {
  const status = tokenStatus.value.status;
  if (status === "refreshed") return "已连接 · 刚刚自动续期";
  if (status === "refresh_failed") return "续期暂时失败";
  if (status === "refresh_configuration_error") return "续期配置有误";
  if (status === "reauthorization_required") return "授权已失效";
  if (status === "expired") return "访问令牌已过期";
  if (status === "configuration_error") return "服务端尚未配置";
  if (tokenValid.value && !tokenStatus.value.refreshable) return "已连接 · 仅本次有效";
  return tokenValid.value ? "已连接 · 自动续期已开启" : "尚未连接";
});
const feishuDescription = computed(() => {
  if (tokenStatus.value.status === "refresh_configuration_error") {
    if (tokenStatus.value.provider_error_code === 20074) {
      return "请由管理员在飞书安全设置中开启“刷新 user_access_token”，发布应用后再重试。";
    }
    if (tokenStatus.value.provider_error_code === 20024) {
      return "保存的 refresh token 不属于当前 App ID；请核对应用凭据后重新授权。";
    }
    return tokenStatus.value.message || "飞书刷新配置不正确，请联系应用管理员。";
  }
  if (tokenStatus.value.needs_reauthorization) {
    return "刷新凭证已失效，需要重新完成一次飞书授权。";
  }
  if (tokenStatus.value.status === "refresh_failed") {
    return "已保存授权，但本次自动续期失败；系统会在下次使用时重试。";
  }
  if (tokenStatus.value.status === "expired") {
    return "访问令牌已经过期；请检查服务端 App Secret 配置后重新连接。";
  }
  if (tokenValid.value && tokenStatus.value.refreshable) {
    return "已绑定当前登录用户授权的飞书账号，访问令牌过期后会自动续期。";
  }
  if (tokenValid.value) {
    return "当前访问令牌可用，但未取得离线刷新权限；过期后需要重新授权。";
  }
  return "连接后读取你自己的主日历；无需填写 user ID、open_id、日历 ID 或 token。";
});
const feishuVerificationLabel = computed(() => {
  if (feishuVerification.value?.valid) {
    return `已验证：${feishuVerification.value.calendar?.summary || "主日历"}可正常读取`;
  }
  if (tokenValid.value) return "授权凭证有效，可点击“检测连接”验证日历读取权限";
  return "完成授权后，这里会显示连接结果";
});
const pageTitle = computed(() => viewLabels[activeView.value]);
const greeting = computed(() => {
  const hour = new Date().getHours();
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
});
const dateLabel = computed(() => {
  const now = new Date();
  const weekdays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
  return `${now.getMonth() + 1}月${now.getDate()}日 · ${weekdays[now.getDay()]}`;
});
const trendPresentation = computed(() => {
  if (!latestResult.value) return { label: "等待首次预测", description: "连接日历或添加日程后查看", width: 18 };
  const score = Number(latestResult.value.end_S || 0);
  return {
    label: score >= 75 ? "负荷偏高" : score >= 55 ? "温和上升" : "相对平稳",
    description: `最近一次日终参考 ${score.toFixed(0)} / 100`,
    width: Math.max(8, Math.min(100, score))
  };
});
const energyPresentation = computed(() => {
  if (!latestResult.value) return { label: "尚未估计", description: "完成问卷后生成作息建议", width: 32 };
  const score = Number(latestResult.value.end_E || 0);
  return {
    label: score < 30 ? "恢复空间较少" : score < 60 ? "需要留意" : "相对充足",
    description: `最近一次精力参考 ${score.toFixed(0)} / 100`,
    width: Math.max(8, Math.min(100, score))
  };
});

function notify(payload) {
  const message = typeof payload === "string" ? payload : payload.message;
  const type = typeof payload === "string" ? "success" : payload.type || "success";
  const id = Date.now() + Math.random();
  toasts.value.push({ id, message, type });
  window.setTimeout(() => {
    toasts.value = toasts.value.filter(item => item.id !== id);
  }, 3600);
}

function handleApiError(error) {
  if (error.status === 401) {
    router.replace("/login");
    return;
  }
  notify({ message: error.message, type: "error" });
}

async function loadDashboard() {
  try {
    dashboard.value = await api("/api/dashboard");
  } catch (error) {
    handleApiError(error);
  }
}

function setView(view) {
  activeView.value = view;
  sidebarOpen.value = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "settings") loadConnections();
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    await router.replace("/login");
  }
}

function openOnboarding() {
  onboardingDialog.value?.open();
}

async function onboardingCompletedHandler() {
  notify("画像已建立。每一项推断都可以查看依据。");
  await loadDashboard();
  setView("profile");
}

function routineTime(item) {
  return item.scheduled_window ? item.scheduled_window.join("–") : "暂未安排";
}

function traitScore(trait) {
  return Math.round(Number(trait.score_0_1 || 0) * 100);
}

function addEvent() {
  const name = predictionForm.eventName.trim();
  if (!name || !predictionForm.eventStart || !predictionForm.eventEnd) {
    notify({ message: "请填写日程名称与时间", type: "error" });
    return;
  }
  if (predictionForm.eventStart >= predictionForm.eventEnd) {
    notify({ message: "结束时间需要晚于开始时间", type: "error" });
    return;
  }
  const overlaps = mockEvents.value.some(
    item => predictionForm.eventStart < item.end && predictionForm.eventEnd > item.start
  );
  if (overlaps) {
    notify({ message: "该时段与已添加日程重叠", type: "error" });
    return;
  }
  mockEvents.value.push({
    name,
    start: predictionForm.eventStart,
    end: predictionForm.eventEnd,
    type: predictionForm.eventType,
    level: predictionForm.eventLevel,
    credit: 2,
    hours: 32,
    intensity: 0.7,
    study_intensity: 0.7
  });
  predictionForm.eventName = "";
}

async function runPrediction() {
  predictionLoading.value = true;
  chartSource.value = "";
  try {
    prediction.value = await api("/api/simulate", {
      method: "POST",
      body: JSON.stringify({
        date: predictionForm.date,
        init_S: Number(predictionForm.initS),
        init_E: Number(predictionForm.initE),
        mock_events: mockEvents.value,
        shield_keywords: [],
        shield_time_ranges: []
      })
    });
    chartSource.value = prediction.value.image
      ? `data:image/png;base64,${prediction.value.image}`
      : "";
    notify("趋势已生成并保存为可重放运行");
    await loadDashboard();
  } catch (error) {
    handleApiError(error);
  } finally {
    predictionLoading.value = false;
  }
}

async function submitFeedback() {
  feedbackLoading.value = true;
  try {
    await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        feedback_type: "momentary_state",
        prediction_run_id: prediction.value?.prediction_run_id || latestRun.value?.prediction_run_id || null,
        target_time: new Date().toISOString(),
        retrospective: false,
        payload: {
          period: feedbackForm.period,
          stress_0_10: Number(feedbackForm.stress),
          energy_0_10: Number(feedbackForm.energy),
          note: feedbackForm.note.trim()
        }
      })
    });
    feedbackForm.note = "";
    notify("已保存此刻感受，谢谢你的记录");
  } catch (error) {
    handleApiError(error);
  } finally {
    feedbackLoading.value = false;
  }
}

async function submitReview() {
  const score = Number(reviewForm.score);
  if (!Number.isFinite(score) || score < 0 || score > 10) {
    notify({ message: "复盘评分需要在 0–10 之间", type: "error" });
    return;
  }
  reviewLoading.value = true;
  try {
    await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        feedback_type: reviewForm.type,
        prediction_run_id: prediction.value?.prediction_run_id || latestRun.value?.prediction_run_id || null,
        target_time: reviewForm.time || null,
        retrospective: true,
        payload: {
          score_0_10: score,
          note_or_correction: reviewForm.note.trim()
        }
      })
    });
    reviewForm.note = "";
    notify("复盘已保存，并与最近一次预测版本关联");
  } catch (error) {
    handleApiError(error);
  } finally {
    reviewLoading.value = false;
  }
}

async function loadConnections() {
  try {
    const [token] = await Promise.all([
      api("/api/token_status"),
      apiKeyDialog.value?.load()
    ]);
    tokenStatus.value = token;
  } catch (error) {
    handleApiError(error);
  }
}

async function connectFeishu() {
  feishuConnecting.value = true;
  try {
    const force = tokenValid.value || tokenStatus.value.needs_reauthorization;
    const payload = await api(`/api/feishu/get_url${force ? "?force=true" : ""}`);
    if (payload.already_connected) {
      tokenStatus.value = payload.connection;
      notify("飞书日历授权仍然有效，无需重复认证");
      return;
    }
    if (payload.missing?.length) {
      notify({ message: `服务端还未配置：${payload.missing.join("、")}`, type: "error" });
      return;
    }
    const popup = window.open(
      payload.url,
      "mindflow-feishu-oauth",
      "popup,width=620,height=760"
    );
    if (!popup) {
      notify({ message: "浏览器阻止了授权窗口，请允许此站点打开弹窗", type: "error" });
      return;
    }
    const popupMonitor = window.setInterval(async () => {
      if (!popup.closed) return;
      window.clearInterval(popupMonitor);
      await loadConnections();
      if (tokenValid.value) await verifyFeishuConnection(false);
    }, 700);
  } catch (error) {
    handleApiError(error);
  } finally {
    feishuConnecting.value = false;
  }
}

async function verifyFeishuConnection(showSuccess = true) {
  feishuChecking.value = true;
  try {
    const result = await api("/api/feishu/verify");
    feishuVerification.value = result;
    await loadConnections();
    if (showSuccess) {
      notify(`飞书连接正常，已读取“${result.calendar?.summary || "我的主日历"}”`);
    }
  } catch (error) {
    feishuVerification.value = { valid: false };
    handleApiError(error);
  } finally {
    feishuChecking.value = false;
  }
}

async function handleFeishuOAuthMessage(event) {
  const devApiOrigin = `${window.location.protocol}//${window.location.hostname}:5000`;
  if (
    ![window.location.origin, devApiOrigin].includes(event.origin) ||
    event.data?.type !== "mindflow:feishu-oauth"
  ) {
    return;
  }
  await loadConnections();
  if (event.data.status === "success" && tokenValid.value) {
    await verifyFeishuConnection(false);
  }
  notify({
    message: event.data.message || (event.data.status === "success" ? "飞书授权成功" : "飞书授权失败"),
    type: event.data.status === "success" ? "success" : "error"
  });
}

onMounted(async () => {
  window.addEventListener("message", handleFeishuOAuthMessage);
  await loadDashboard();
  loading.value = false;
});

onUnmounted(() => {
  window.removeEventListener("message", handleFeishuOAuthMessage);
});
</script>

<template>
  <div class="app-page">
    <div class="app-shell">
      <aside class="sidebar" :class="{ open: sidebarOpen }">
        <a class="brand" href="/" @click.prevent="setView('dashboard')">
          <span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span>
          <span><strong>心序</strong><small>MindFlow</small></span>
        </a>

        <nav class="side-nav" aria-label="主导航">
          <p>我的空间</p>
          <button class="nav-item" :class="{ active: activeView === 'dashboard' }" type="button" @click="setView('dashboard')">
            <span class="nav-icon home-icon"></span><b>今日概览</b>
          </button>
          <button class="nav-item" :class="{ active: activeView === 'profile' }" type="button" @click="setView('profile')">
            <span class="nav-icon profile-icon"></span><b>我的画像</b>
          </button>
          <button class="nav-item" :class="{ active: activeView === 'prediction' }" type="button" @click="setView('prediction')">
            <span class="nav-icon trend-icon"></span><b>趋势预测</b>
          </button>
          <button class="nav-item" :class="{ active: activeView === 'feedback' }" type="button" @click="setView('feedback')">
            <span class="nav-icon pulse-icon"></span><b>轻量反馈</b>
          </button>
          <p>连接与偏好</p>
          <button class="nav-item" :class="{ active: activeView === 'settings' }" type="button" @click="setView('settings')">
            <span class="nav-icon settings-icon"></span><b>设置</b>
          </button>
          <template v-if="isAdmin">
            <p>管理工具</p>
            <button class="nav-item" :class="{ active: activeView === 'admin' }" type="button" @click="setView('admin')">
              <span class="nav-icon admin-icon"></span><b>管理与诊断</b>
            </button>
          </template>
        </nav>

        <div class="sidebar-principle">
          <span>今日一句</span>
          <p>不必把所有事情都安排妥当，给变化留一点余地。</p>
        </div>
        <div class="sidebar-user">
          <div class="avatar">{{ userLabel.slice(0, 1).toUpperCase() }}</div>
          <div><strong>{{ userLabel }}</strong><span>{{ user.login_type === "email" ? "邮箱账号" : "学号账号" }}</span></div>
          <button type="button" title="退出登录" aria-label="退出登录" @click="logout">↗</button>
        </div>
      </aside>

      <main class="main-content">
        <header class="topbar">
          <button class="mobile-menu" type="button" aria-label="打开导航" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div><p>{{ dateLabel }}</p><h1>{{ pageTitle }}</h1></div>
          <div class="topbar-actions">
            <span class="sync-state"><i></i> 本地数据已就绪</span>
            <button class="quiet-button" type="button" @click="setView('feedback')">记录此刻</button>
          </div>
        </header>

        <section v-show="activeView === 'dashboard'" class="view active">
          <div v-if="!onboardingCompleted" class="onboarding-banner">
            <div class="banner-art" aria-hidden="true"><span></span><span></span><span></span></div>
            <div>
              <p class="eyebrow light">建立你的初始节律</p>
              <h2>先花 3 分钟，让建议更贴近你</h2>
              <p>回答一组简单问题，系统会生成可查看、可修改的初始画像。</p>
            </div>
            <button class="light-button" type="button" @click="openOnboarding">开始问卷 <b>→</b></button>
          </div>

          <div class="welcome-row">
            <div>
              <p class="eyebrow">WELCOME BACK</p>
              <h2>{{ greeting }}，<span>{{ userLabel }}</span></h2>
              <p>今天也从理解自己的节奏开始。</p>
            </div>
            <div class="model-note">
              <span>模型说明</span>
              <p>结果表示日程负荷趋势，不等同于真实心理状态或诊断。</p>
            </div>
          </div>

          <div class="metric-grid">
            <article class="metric-card calm-card">
              <div class="metric-top"><span>今日趋势</span><i class="metric-symbol">∿</i></div>
              <strong>{{ trendPresentation.label }}</strong>
              <p>{{ trendPresentation.description }}</p>
              <div class="soft-meter"><span :style="{ width: `${trendPresentation.width}%` }"></span></div>
            </article>
            <article class="metric-card energy-card">
              <div class="metric-top"><span>恢复空间</span><i class="metric-symbol">◒</i></div>
              <strong>{{ energyPresentation.label }}</strong>
              <p>{{ energyPresentation.description }}</p>
              <div class="soft-meter"><span :style="{ width: `${energyPresentation.width}%` }"></span></div>
            </article>
            <article class="metric-card profile-card-mini">
              <div class="metric-top"><span>画像状态</span><i class="metric-symbol">◎</i></div>
              <strong>{{ onboardingCompleted ? "已建立" : "待初始化" }}</strong>
              <p>{{ onboardingCompleted ? "初始画像已就绪，可查看推断依据" : "完成问卷后生成可审计画像" }}</p>
              <button class="text-button" type="button" @click="setView('profile')">查看依据 →</button>
            </article>
          </div>

          <div class="dashboard-grid">
            <article class="surface-card rhythm-card">
              <div class="card-heading">
                <div><p class="eyebrow">TODAY'S RHYTHM</p><h3>今天的节律安排</h3></div>
                <button class="text-button" type="button" @click="setView('prediction')">调整与预测 →</button>
              </div>
              <div class="routine-timeline">
                <div v-if="!routinePlan?.items?.length" class="empty-state compact">
                  <span>○</span><p>完成问卷后，这里会显示午餐、午睡与晚餐的建议时间。</p>
                </div>
                <div v-else class="routine-items">
                  <div
                    v-for="item in routinePlan.items"
                    :key="item.routine_type"
                    class="routine-item"
                    :class="{ unavailable: !item.scheduled_window }"
                  >
                    <time>{{ routineTime(item) }}</time>
                    <div>
                      <strong>{{ routineLabels[item.routine_type] || item.routine_type }}</strong>
                      <small>{{ item.scheduled_window ? "来自问卷作息偏好" : "不会自动创建恢复事件" }}</small>
                    </div>
                    <em>{{ routineStatusLabels[item.status] || item.status }}</em>
                  </div>
                </div>
              </div>
            </article>

            <article class="surface-card care-card">
              <div class="care-orb" aria-hidden="true"></div>
              <p class="eyebrow">GENTLE NOTE</p>
              <h3>留一点缓冲，<br>也是一种前进。</h3>
              <p>系统会在发现可能的高负荷时段后，给出一条简短、可忽略的建议。</p>
              <small>你随时可以在设置里关闭关怀提示</small>
            </article>
          </div>

          <article class="surface-card recent-card">
            <div class="card-heading">
              <div><p class="eyebrow">RECENT RUNS</p><h3>最近的趋势记录</h3></div>
              <span class="version-chip">{{ versions.model || "基线模型" }}</span>
            </div>
            <div v-if="!recentRuns.length" class="empty-state">
              <span>⌁</span><h4>还没有趋势记录</h4><p>运行一次今日预测后，结果会以可重放版本保存在这里。</p>
            </div>
            <div v-else>
              <div v-for="run in recentRuns" :key="run.prediction_run_id" class="recent-run-item">
                <time>{{ run.local_date }}</time><strong>{{ run.model_version }}</strong>
                <span>压力 {{ Number(run.result?.end_S || 0).toFixed(0) }}</span>
                <span>精力 {{ Number(run.result?.end_E || 0).toFixed(0) }}</span>
                <span>{{ run.result?.alerts?.length || 0 }} 条提示</span>
              </div>
            </div>
          </article>
        </section>

        <section v-show="activeView === 'profile'" class="view active">
          <div class="page-intro">
            <p class="eyebrow">YOUR PATTERN</p><h2>我的初始画像</h2>
            <p>画像来自多题规则推断，不是人格标签，也不会被当作真实心理状态。</p>
          </div>
          <div v-if="!profile" class="surface-card empty-state tall">
            <span>◎</span><h3>先认识一下你的日常节律</h3>
            <p>完成约 3 分钟的初始化问卷，即可查看画像维度与每项依据。</p>
            <button class="primary-button" type="button" @click="openOnboarding">开始问卷</button>
          </div>
          <template v-else>
            <article class="profile-hero">
              <div>
                <p class="eyebrow light">PROFILE SNAPSHOT</p><h3>你的节律画像</h3><p>{{ profile.summary }}</p>
              </div>
              <div class="profile-quality">
                <span>规则推断</span><strong>{{ profile.mapping_version }}</strong><small>可审计 · 可重新填写</small>
              </div>
            </article>
            <div class="trait-grid">
              <article v-for="trait in profile.traits" :key="trait.trait" class="trait-card">
                <div class="trait-score" :style="{ '--score': `${traitScore(trait)}%` }"><b>{{ traitScore(trait) }}</b></div>
                <h4>{{ trait.label || trait.trait }}</h4>
                <p>推断置信度 {{ Math.round(Number(trait.confidence_0_1 || 0) * 100) }}% · 分数仅作为模型先验</p>
                <details><summary>查看依据</summary>
                  <ul><li v-for="item in trait.evidence" :key="item.question_id">{{ item.question_id }} · {{ item.role }}</li></ul>
                </details>
              </article>
            </div>
            <article class="surface-card">
              <div class="card-heading">
                <div><p class="eyebrow">PARAMETER PRIORS</p><h3>模型先验建议</h3></div>
                <span class="status-pill safe">仅建议，未自动应用</span>
              </div>
              <div class="prior-list">
                <div v-for="prior in profile.parameter_priors" :key="prior.parameter" class="prior-item">
                  <strong>{{ prior.parameter }}</strong><b>{{ Number(prior.mean).toFixed(2) }}</b>
                  <div class="prior-range" :title="`${prior.lower} – ${prior.upper}`"><span></span></div>
                </div>
              </div>
            </article>
            <button class="outline-button profile-retake" type="button" @click="openOnboarding">重新填写问卷</button>
          </template>
          <StrategyPreferences @notify="notify" @updated="loadDashboard" />
        </section>

        <section v-show="activeView === 'prediction'" class="view active">
          <div class="page-intro prediction-intro">
            <div>
              <p class="eyebrow">TREND ESTIMATE</p><h2>日程趋势预测</h2>
              <p>曲线是内部模型的展示副本；原始状态点会按运行版本完整保存。</p>
            </div>
            <span class="status-pill safe">非诊断性趋势</span>
          </div>
          <div class="prediction-layout">
            <aside class="surface-card prediction-controls">
              <div class="control-section">
                <span class="step-label">日期</span>
                <input v-model="predictionForm.date" class="clean-input" type="date">
              </div>
              <div class="control-section">
                <span class="step-label">起始状态</span>
                <label>主观压力参考 <b>{{ predictionForm.initS }}</b></label>
                <input v-model="predictionForm.initS" type="range" min="0" max="100">
                <label>精力参考 <b>{{ predictionForm.initE }}</b></label>
                <input v-model="predictionForm.initE" type="range" min="0" max="100">
              </div>
              <div class="control-section">
                <span class="step-label">添加一个日程</span>
                <input v-model="predictionForm.eventName" class="clean-input" placeholder="例如：毕业答辩">
                <div class="two-fields">
                  <select v-model="predictionForm.eventType" class="clean-input">
                    <option value="task">任务</option><option value="course">课程</option>
                    <option value="gym">运动</option><option value="library">自习</option><option value="rest">休息</option>
                  </select>
                  <select v-model="predictionForm.eventLevel" class="clean-input">
                    <option value="general">一般</option><option value="ddl">截止任务</option>
                    <option value="exam">考试/答辩</option><option value="meeting">会议</option>
                  </select>
                </div>
                <div class="two-fields">
                  <input v-model="predictionForm.eventStart" class="clean-input" type="time">
                  <input v-model="predictionForm.eventEnd" class="clean-input" type="time">
                </div>
                <button class="outline-button full-button" type="button" @click="addEvent">＋ 加入日程</button>
                <div class="event-list">
                  <div v-for="(event, index) in mockEvents" :key="`${event.name}-${index}`" class="event-chip">
                    <div><strong>{{ event.name }}</strong><span>{{ event.start }}–{{ event.end }} · {{ event.type }}</span></div>
                    <button type="button" aria-label="移除" @click="mockEvents.splice(index, 1)">×</button>
                  </div>
                </div>
              </div>
              <button class="primary-button full-button" type="button" :disabled="predictionLoading" @click="runPrediction">
                <span :class="{ 'loading-inline': predictionLoading }">{{ predictionLoading ? "正在生成趋势" : "生成今日趋势" }}</span><b>→</b>
              </button>
            </aside>

            <div class="prediction-results">
              <article class="surface-card chart-card">
                <div class="card-heading">
                  <div><p class="eyebrow">MODEL TRAJECTORY</p><h3>压力与精力趋势</h3></div>
                  <span class="version-chip">{{ prediction ? `指纹 ${prediction.input_fingerprint.slice(0, 10)}` : "尚未运行" }}</span>
                </div>
                <div class="chart-area">
                  <img v-if="chartSource" :src="chartSource" alt="当天压力与精力趋势图">
                  <div v-else class="empty-state">
                    <span>∿</span><h4>{{ predictionLoading ? "正在整理日程与节律" : "准备好后，生成今天的趋势" }}</h4>
                    <p>同一输入、参数和种子会得到相同结果，重放不会修改你的画像。</p>
                  </div>
                </div>
              </article>
              <div v-if="prediction" class="result-metrics">
                <article><span>日终压力参考</span><strong>{{ Number(prediction.end_S).toFixed(1) }}</strong></article>
                <article><span>日终精力参考</span><strong>{{ Number(prediction.end_E).toFixed(1) }}</strong></article>
                <article><span>风险提示</span><strong>{{ prediction.alerts?.length || 0 }} 条</strong></article>
              </div>
              <article v-if="prediction?.alerts?.length" class="surface-card">
                <div class="card-heading"><div><p class="eyebrow">RISK WINDOWS</p><h3>值得留意的时段</h3></div></div>
                <div class="alert-list">
                  <div v-for="(alert, index) in prediction.alerts" :key="index" class="alert-item">
                    <strong>{{ alert.type || "趋势提示" }}</strong>
                    <p>{{ alert.message || alert.reason || JSON.stringify(alert) }}</p>
                  </div>
                </div>
              </article>
              <details v-if="prediction" class="surface-card technical-details">
                <summary>查看研究与复现信息</summary>
                <div class="technical-grid">
                  <div><span>模型版本</span><strong>{{ prediction.versions.model }}</strong></div>
                  <div><span>参数版本</span><strong>{{ prediction.versions.parameters }}</strong></div>
                  <div><span>特征版本</span><strong>{{ prediction.versions.features }}</strong></div>
                  <div><span>运行 ID</span><strong>{{ prediction.prediction_run_id }}</strong></div>
                </div>
              </details>
            </div>
          </div>
        </section>

        <section v-show="activeView === 'feedback'" class="view active">
          <div class="page-intro">
            <p class="eyebrow">LIGHT CHECK-IN</p><h2>今天，此刻怎么样？</h2>
            <p>只需几秒。时点反馈会与预测版本关联，用于后续校准，而不是自动改变画像。</p>
          </div>
          <div class="feedback-grid">
            <article class="feedback-form surface-card">
              <div class="feedback-period" role="group" aria-label="反馈时间段">
                <button v-for="period in [['morning','早晨'],['noon','中午'],['evening','晚上']]"
                        :key="period[0]" :class="{ active: feedbackForm.period === period[0] }"
                        type="button" @click="feedbackForm.period = period[0]">{{ period[1] }}</button>
              </div>
              <label class="scale-question"><span>此刻的压力感受</span><b>{{ feedbackForm.stress }}</b></label>
              <input v-model="feedbackForm.stress" type="range" min="0" max="10">
              <div class="scale-ends"><span>很轻松</span><span>非常紧绷</span></div>
              <label class="scale-question"><span>此刻的精力状态</span><b>{{ feedbackForm.energy }}</b></label>
              <input v-model="feedbackForm.energy" type="range" min="0" max="10">
              <div class="scale-ends"><span>几乎耗尽</span><span>精力充足</span></div>
              <label class="field-label">想补充一句吗？（选填）</label>
              <textarea v-model="feedbackForm.note" class="clean-input" rows="3" placeholder="例如：刚结束一场汇报，正在慢慢放松。"></textarea>
              <button class="primary-button full-button" type="button" :disabled="feedbackLoading" @click="submitFeedback">
                {{ feedbackLoading ? "保存中…" : "保存此刻感受" }}
              </button>
            </article>
            <aside class="surface-card feedback-guide">
              <div class="guide-orb">○</div><h3>少一点打扰，<br>多一点有效信息。</h3>
              <p>早、中、晚各一次已经足够。系统会记录填写时间，回顾性反馈也会明确标记。</p>
              <ul><li><span></span>0–10 量表统一保存</li><li><span></span>关联最近一次预测运行</li><li><span></span>原始反馈不会被静默覆盖</li></ul>
            </aside>
          </div>
          <article class="surface-card review-card">
            <div class="card-heading">
              <div><p class="eyebrow">AFTERWARD REVIEW</p><h3>补充一次事件或预测复盘</h3></div>
              <span class="status-pill">选填 · 约 30 秒</span>
            </div>
            <div class="review-form">
              <label><span>复盘内容</span>
                <select v-model="reviewForm.type" class="clean-input">
                  <option value="peak_review">今天的实际峰值</option><option value="event_impact">关键事件影响 / 纠错</option>
                  <option value="prediction_review">预警是否准确</option><option value="care_review">关怀是否有帮助</option>
                  <option value="routine_correction">实际作息纠错</option>
                </select>
              </label>
              <label><span>相关时间</span><input v-model="reviewForm.time" class="clean-input" type="time"></label>
              <label><span>{{ reviewLabels[reviewForm.type] }}</span><input v-model="reviewForm.score" class="clean-input" type="number" min="0" max="10"></label>
              <label class="review-note-field"><span>补充说明 / 事件名称 / 纠错内容</span>
                <input v-model="reviewForm.note" class="clean-input" placeholder="例如：答辩比预期轻松；该事件应归为会议">
              </label>
              <button class="outline-button" type="button" :disabled="reviewLoading" @click="submitReview">{{ reviewLoading ? "保存中" : "保存复盘" }}</button>
            </div>
          </article>
        </section>

        <section v-show="activeView === 'settings'" class="view active">
          <div class="page-intro">
            <p class="eyebrow">SETTINGS</p><h2>连接与偏好</h2>
            <p>账号连接、开发者密钥和模型版本集中在这里，避免与日常界面混在一起。</p>
          </div>
          <div class="settings-grid">
            <article class="surface-card settings-card">
              <div class="settings-icon connection">⌁</div>
              <div class="settings-copy">
                <h3>飞书日历</h3><p>{{ feishuDescription }}</p>
                <span class="status-pill" :class="tokenValid ? 'safe' : 'warning'">{{ feishuStatusLabel }}</span>
                <small class="connection-check-copy" :class="{ verified: feishuVerification?.valid }">
                  {{ feishuVerificationLabel }}
                </small>
                <small v-if="feishuRedirectUri" class="oauth-config-copy">
                  飞书应用 <code>{{ feishuOauthAppId }}</code> 的安全设置必须登记：
                  <code>{{ feishuRedirectUri }}</code>
                </small>
              </div>
              <div class="connection-actions">
                <button v-if="tokenValid" class="outline-button subtle" type="button" :disabled="feishuChecking" @click="verifyFeishuConnection()">
                  {{ feishuChecking ? "检测中…" : "检测连接" }}
                </button>
                <button class="outline-button" type="button" :disabled="feishuConnecting" @click="connectFeishu">
                  {{ feishuConnecting ? "处理中…" : tokenValid ? "重新授权" : "连接日历" }}
                </button>
              </div>
            </article>
            <article class="surface-card settings-card">
              <div class="settings-icon key">◇</div>
              <div class="settings-copy">
                <h3>开发者 API Key</h3><p>用于脚本或研究客户端调用。密钥只在创建时显示一次。</p>
                <span class="status-pill safe">{{ activeKeyCount }} 个有效密钥</span>
              </div>
              <button class="outline-button" type="button" @click="apiKeyDialog.open()">管理密钥</button>
            </article>
            <article class="surface-card version-panel">
              <div class="card-heading">
                <div><p class="eyebrow">BASELINE</p><h3>当前模型基线</h3></div><span class="status-pill safe">阶段 0 已冻结</span>
              </div>
              <div class="version-rows">
                <div><span>模型</span><strong>{{ versions.model || "—" }}</strong></div>
                <div><span>参数</span><strong>{{ versions.parameters || "—" }}</strong></div>
                <div><span>事件特征</span><strong>{{ versions.features || "—" }}</strong></div>
              </div>
              <div class="baseline-note">半马尔可夫、泊松异常、顿悟/多巴胺、压力动量与日际自动演进默认关闭。</div>
            </article>
          </div>
        </section>

        <section v-if="isAdmin && activeView === 'admin'" class="view active admin-view-shell">
          <AdminView @notify="notify" />
        </section>
      </main>
    </div>

    <OnboardingDialog ref="onboardingDialog" :profile="profile" @completed="onboardingCompletedHandler" @notify="notify" />
    <ApiKeyDialog ref="apiKeyDialog" @notify="notify" @updated="activeKeyCount = $event" />

    <div class="toast-stack" aria-live="polite">
      <div v-for="item in toasts" :key="item.id" class="toast" :class="{ error: item.type === 'error' }">{{ item.message }}</div>
    </div>
    <Transition name="vue-fade">
      <div v-if="loading" class="page-loading"><span></span><p>正在整理你的空间…</p></div>
    </Transition>
  </div>
</template>
