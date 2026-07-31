<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api";

const emit = defineEmits(["notify", "updated"]);

const loading = ref(true);
const saving = ref(false);
const payload = ref(null);
const draft = reactive({});

const families = computed(() => payload.value?.families || []);
const hasChanges = computed(() =>
  families.value.some((family) => draft[family.key] !== family.current)
);

function syncDraft() {
  Object.entries(payload.value?.current || {}).forEach(([key, value]) => {
    draft[key] = value;
  });
}

function selectedChoice(family) {
  return family.choices.find((choice) => choice.value === draft[family.key]);
}

async function load() {
  loading.value = true;
  try {
    const result = await api("/api/profile/strategies");
    payload.value = result.strategies;
    syncDraft();
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    loading.value = false;
  }
}

async function save() {
  if (!hasChanges.value || saving.value) return;
  saving.value = true;
  try {
    const strategies = Object.fromEntries(
      families.value.map((family) => [family.key, draft[family.key]])
    );
    const result = await api("/api/profile/strategies", {
      method: "PATCH",
      body: JSON.stringify({ strategies })
    });
    payload.value = result.strategies;
    syncDraft();
    emit("updated", result.strategies);
    emit("notify", "策略已保存，下一次趋势计算会使用这些选择。");
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <article class="surface-card strategy-preferences">
    <div class="card-heading strategy-heading">
      <div>
        <p class="eyebrow">MODEL BEHAVIOUR</p>
        <h3>我的计算策略</h3>
        <p>这些选项决定模型曲线如何响应负荷与休息，可随时修改。</p>
      </div>
      <div class="strategy-actions">
        <span class="status-pill warning">不是心理诊断</span>
        <button
          class="primary-button compact"
          type="button"
          :disabled="loading || saving || !hasChanges"
          @click="save"
        >
          {{ saving ? "保存中…" : hasChanges ? "保存修改" : "已保存" }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="strategy-loading">正在读取你的策略…</div>
    <template v-else-if="payload">
      <p class="strategy-notice">{{ payload.notice }}</p>
      <div class="strategy-grid">
        <section v-for="family in families" :key="family.key" class="strategy-family">
          <div class="strategy-family-title">
            <div>
              <span>{{ family.short_label }}</span>
              <h4>{{ family.label }}</h4>
            </div>
            <select v-model="draft[family.key]" class="clean-input strategy-select">
              <option
                v-for="choice in family.choices"
                :key="choice.value"
                :value="choice.value"
              >
                {{ choice.label }}
              </option>
            </select>
          </div>
          <p>{{ family.description }}</p>
          <div v-if="selectedChoice(family)" class="strategy-explanation">
            <strong>{{ selectedChoice(family).label }}</strong>
            <span>{{ selectedChoice(family).summary }}</span>
            <small>{{ selectedChoice(family).effect }}</small>
          </div>
          <details>
            <summary>比较全部选项</summary>
            <ul>
              <li v-for="choice in family.choices" :key="choice.value">
                <b>{{ choice.label }}</b>
                <span>{{ choice.summary }}</span>
              </li>
            </ul>
          </details>
        </section>
      </div>
    </template>
  </article>
</template>
