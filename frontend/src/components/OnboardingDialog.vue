<script setup>
import { computed, nextTick, reactive, ref } from "vue";
import { api } from "../api";

const props = defineProps({
  profile: { type: Object, default: null }
});
const emit = defineEmits(["completed", "notify"]);

const dialog = ref(null);
const questionnaire = ref(null);
const step = ref(0);
const answers = reactive({});
const errorMessage = ref("");
const submitting = ref(false);

const sections = computed(() => questionnaire.value?.sections || []);
const currentSection = computed(() => sections.value[step.value] || null);
const progress = computed(() => sections.value.length ? ((step.value + 1) / sections.value.length) * 100 : 0);
const timeQuestions = computed(() => currentSection.value?.questions.filter(q => q.response_type === "local_time") || []);
const otherQuestions = computed(() => currentSection.value?.questions.filter(q => q.response_type !== "local_time") || []);

function initializeAnswers() {
  Object.keys(answers).forEach(key => delete answers[key]);
  const existing = props.profile
    ? {
        ...(props.profile.routine || {}),
        support_style: props.profile.care_preferences?.preferred_support || [],
        care_tone: props.profile.care_preferences?.tone || "brief_warm"
      }
    : {};
  for (const section of sections.value) {
    for (const question of section.questions) {
      const fallback = question.response_type === "multiple_choice" ? [] : "";
      answers[question.question_id] = existing[question.question_id] ?? question.default ?? fallback;
    }
  }
}

async function open() {
  errorMessage.value = "";
  step.value = 0;
  try {
    if (!questionnaire.value) {
      const payload = await api("/api/onboarding/questionnaire");
      questionnaire.value = payload.questionnaire;
    }
    initializeAnswers();
    await nextTick();
    dialog.value.showModal();
    document.documentElement.classList.add("vue-dialog-open");
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  }
}

function close() {
  dialog.value?.close();
  document.documentElement.classList.remove("vue-dialog-open");
}

function handleDialogClose() {
  document.documentElement.classList.remove("vue-dialog-open");
}

function validateCurrent() {
  const missing = currentSection.value.questions.filter(question => {
    if (!question.required) return false;
    const value = answers[question.question_id];
    return value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
  });
  errorMessage.value = missing.length ? `还有 ${missing.length} 项没有回答，请完成后继续。` : "";
  return missing.length === 0;
}

function previous() {
  if (step.value > 0) {
    errorMessage.value = "";
    step.value -= 1;
  }
}

function next() {
  if (!validateCurrent()) return;
  if (step.value < sections.value.length - 1) step.value += 1;
}

async function submit() {
  if (!validateCurrent()) return;
  submitting.value = true;
  try {
    const payload = await api("/api/onboarding/responses", {
      method: "POST",
      body: JSON.stringify({
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
        answers
      })
    });
    close();
    emit("completed", payload);
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    submitting.value = false;
  }
}

defineExpose({ open, close });
</script>

<template>
  <dialog ref="dialog" class="onboarding-dialog" @close="handleDialogClose">
    <div class="dialog-shell">
      <header class="dialog-header">
        <a class="brand" href="#" tabindex="-1" @click.prevent>
          <span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span>
          <span><strong>心序</strong><small>初始画像</small></span>
        </a>
        <div class="question-progress">
          <span>第 {{ step + 1 }} / {{ sections.length }} 步</span>
          <div><i :style="{ width: `${progress}%` }"></i></div>
        </div>
        <button class="dialog-close" type="button" aria-label="关闭" @click="close">×</button>
      </header>

      <div v-if="currentSection" class="dialog-body">
        <aside class="question-aside">
          <p class="eyebrow light">ABOUT THIS</p>
          <h2>不是测试，<br>只是一次认识。</h2>
          <p>没有标准答案。我们只用这些信息建立初始作息与敏感度先验。</p>
          <div class="question-promise">
            <span>01</span><p>原始答案独立保存</p>
            <span>02</span><p>每项推断都可查看依据</p>
            <span>03</span><p>不会自动生成诊断标签</p>
          </div>
        </aside>

        <section class="question-main">
          <div class="section-heading">
            <p class="eyebrow">{{ currentSection.eyebrow }}</p>
            <h2>{{ currentSection.title }}</h2>
            <p>{{ currentSection.description }}</p>
          </div>
          <div v-if="errorMessage" class="question-error">{{ errorMessage }}</div>

          <div v-if="timeQuestions.length" class="time-question-grid">
            <div v-for="question in timeQuestions" :key="question.question_id" class="question-field">
              <label :for="`q_${question.question_id}`">{{ question.prompt }}</label>
              <input
                :id="`q_${question.question_id}`"
                v-model="answers[question.question_id]"
                class="clean-input"
                type="time"
                :required="question.required"
              >
            </div>
          </div>

          <div v-for="question in otherQuestions" :key="question.question_id" class="question-field">
            <fieldset v-if="question.response_type === 'likert_1_5'">
              <legend>{{ question.prompt }}</legend>
              <div class="likert-options">
                <label v-for="score in [1,2,3,4,5]" :key="score" class="likert-option">
                  <input v-model="answers[question.question_id]" type="radio" :name="question.question_id" :value="score">
                  <span>{{ score }}</span>
                </label>
              </div>
              <div class="likert-labels"><span>完全不符合</span><span>非常符合</span></div>
            </fieldset>

            <fieldset v-else-if="question.response_type === 'single_choice' || question.response_type === 'multiple_choice'">
              <legend>{{ question.prompt }}</legend>
              <div class="choice-options">
                <label v-for="option in question.options" :key="option.value" class="choice-option">
                  <input
                    v-if="question.response_type === 'multiple_choice'"
                    v-model="answers[question.question_id]"
                    type="checkbox"
                    :value="option.value"
                  >
                  <input
                    v-else
                    v-model="answers[question.question_id]"
                    type="radio"
                    :name="question.question_id"
                    :value="option.value"
                  >
                  <span>{{ option.label }}</span>
                </label>
              </div>
            </fieldset>

            <template v-else>
              <label :for="`q_${question.question_id}`">{{ question.prompt }}</label>
              <textarea
                :id="`q_${question.question_id}`"
                v-model="answers[question.question_id]"
                class="clean-input"
                rows="3"
                :placeholder="question.help || ''"
              ></textarea>
              <small v-if="question.help" class="question-help">{{ question.help }}</small>
            </template>
          </div>

          <div class="question-actions">
            <button v-if="step > 0" class="outline-button" type="button" @click="previous">上一步</button>
            <button v-if="step < sections.length - 1" class="primary-button" type="button" @click="next">
              下一步 <b>→</b>
            </button>
            <button v-else class="primary-button" type="button" :disabled="submitting" @click="submit">
              <span :class="{ 'loading-inline': submitting }">{{ submitting ? "正在生成" : "生成我的画像" }}</span>
              <b>→</b>
            </button>
          </div>
        </section>
      </div>
    </div>
  </dialog>
</template>
