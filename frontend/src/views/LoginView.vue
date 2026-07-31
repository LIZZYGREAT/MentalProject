<script setup>
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";

const router = useRouter();
const mode = ref("login");
const loginId = ref("");
const password = ref("");
const revealPassword = ref(false);
const loading = ref(false);
const errorMessage = ref("");

function setMode(nextMode) {
  mode.value = nextMode;
  errorMessage.value = "";
}

async function submit() {
  errorMessage.value = "";
  const normalizedLoginId = loginId.value.trim();
  const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedLoginId);
  const isStudentId =
    /^[A-Za-z0-9][A-Za-z0-9_-]{4,31}$/.test(normalizedLoginId) &&
    /\d/.test(normalizedLoginId);
  if (!isEmail && !isStudentId) {
    errorMessage.value = "请输入有效邮箱，或 5–32 位学号";
    return;
  }
  if (password.value.length < 10) {
    errorMessage.value = "密码至少需要 10 个字符";
    return;
  }
  loading.value = true;
  try {
    await api(`/api/auth/${mode.value}`, {
      method: "POST",
      body: JSON.stringify({
        login_id: normalizedLoginId,
        password: password.value
      })
    });
    await router.replace("/");
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  try {
    await api("/api/auth/me");
    await router.replace("/");
  } catch {
    // A missing session is the normal state for the login screen.
  }
});
</script>

<template>
  <div class="login-page">
    <main class="auth-shell">
      <section class="auth-story" aria-label="产品介绍">
        <a class="brand brand-light" href="/" @click.prevent>
          <span class="brand-mark" aria-hidden="true">
            <span></span><span></span><span></span>
          </span>
          <span><strong>心序</strong><small>MindFlow</small></span>
        </a>

        <div class="story-copy">
          <p class="eyebrow light">把一天，放回自己的节奏里</p>
          <h1>看见压力的趋势，<br>也看见恢复的空间。</h1>
          <p>从日程与轻量反馈中提前识别可能的高负荷时段，给你克制、温和、可以忽略的支持。</p>
        </div>

        <div class="story-preview" aria-hidden="true">
          <div class="preview-orbit orbit-one"></div>
          <div class="preview-orbit orbit-two"></div>
          <div class="preview-card">
            <span class="preview-label">今日节律</span>
            <div class="preview-line">
              <i style="height:34%"></i><i style="height:48%"></i>
              <i style="height:42%"></i><i style="height:68%"></i>
              <i style="height:57%"></i><i style="height:38%"></i>
              <i style="height:28%"></i>
            </div>
            <p><span></span>下午 14:00 前后，给自己留一点缓冲</p>
          </div>
        </div>
        <p class="story-footnote">趋势参考，不用于诊断或替代专业帮助</p>
      </section>

      <section class="auth-panel">
        <div class="auth-panel-inner">
          <div class="mobile-brand">
            <span class="brand-mark" aria-hidden="true">
              <span></span><span></span><span></span>
            </span>
            <strong>心序 MindFlow</strong>
          </div>

          <div class="auth-heading">
            <p class="eyebrow">{{ mode === "login" ? "欢迎回来" : "开始使用" }}</p>
            <h2>{{ mode === "login" ? "登录你的空间" : "创建你的个人空间" }}</h2>
            <p>
              {{ mode === "login"
                ? "继续查看今天的节律与支持建议。"
                : "从一份简短问卷开始，建立只属于你的日常节律。" }}
            </p>
          </div>

          <div class="auth-tabs" role="tablist" aria-label="账号操作">
            <button class="auth-tab" :class="{ active: mode === 'login' }" type="button" @click="setMode('login')">登录</button>
            <button class="auth-tab" :class="{ active: mode === 'register' }" type="button" @click="setMode('register')">创建账号</button>
          </div>

          <form novalidate @submit.prevent="submit">
            <label class="field-label" for="login-id">邮箱或学号</label>
            <div class="field-shell">
              <span class="field-icon" aria-hidden="true">ID</span>
              <input
                id="login-id"
                v-model="loginId"
                name="login_id"
                autocomplete="username"
                maxlength="254"
                placeholder="name@school.edu.cn 或学号"
                required
                autofocus
              >
            </div>

            <div class="password-label">
              <label class="field-label" for="password">密码</label>
              <span>至少 10 个字符</span>
            </div>
            <div class="field-shell">
              <span class="field-icon lock-icon" aria-hidden="true"></span>
              <input
                id="password"
                v-model="password"
                name="password"
                :type="revealPassword ? 'text' : 'password'"
                :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
                minlength="10"
                placeholder="输入密码"
                required
              >
              <button
                class="reveal-button"
                type="button"
                :aria-label="revealPassword ? '隐藏密码' : '显示密码'"
                @click="revealPassword = !revealPassword"
              >
                {{ revealPassword ? "隐藏" : "显示" }}
              </button>
            </div>

            <div v-if="errorMessage" class="inline-alert error" role="alert">{{ errorMessage }}</div>
            <button class="primary-button auth-submit" type="submit" :disabled="loading">
              <span :class="{ 'loading-inline': loading }">
                {{ loading ? "请稍候" : mode === "register" ? "创建账号" : "登录" }}
              </span>
              <b aria-hidden="true">→</b>
            </button>
          </form>

          <div class="privacy-note">
            <span aria-hidden="true">◇</span>
            <p>你的问卷原答与模型推断分层保存。预测只提供趋势参考，不会给出医学诊断。</p>
          </div>
        </div>
      </section>
    </main>
  </div>
</template>
