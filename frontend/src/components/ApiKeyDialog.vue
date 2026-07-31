<script setup>
import { computed, ref } from "vue";
import { api } from "../api";

const emit = defineEmits(["notify", "updated"]);
const dialog = ref(null);
const keys = ref([]);
const name = ref("");
const expiresDays = ref(30);
const newKey = ref("");
const loading = ref(false);

const activeCount = computed(() => keys.value.filter(key => !key.revoked_at).length);

async function load() {
  const payload = await api("/api/auth/api-keys");
  keys.value = payload.api_keys || [];
  emit("updated", activeCount.value);
}

async function open() {
  try {
    await load();
    dialog.value.showModal();
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  }
}

function close() {
  dialog.value?.close();
  newKey.value = "";
}

async function createKey() {
  if (!name.value.trim()) {
    emit("notify", { message: "请填写密钥名称", type: "error" });
    return;
  }
  loading.value = true;
  try {
    const payload = await api("/api/auth/api-keys", {
      method: "POST",
      body: JSON.stringify({
        name: name.value.trim(),
        expires_days: Number(expiresDays.value)
      })
    });
    newKey.value = payload.api_key.key;
    name.value = "";
    await load();
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  } finally {
    loading.value = false;
  }
}

async function revokeKey(id) {
  try {
    await api(`/api/auth/api-keys/${id}`, { method: "DELETE" });
    await load();
    emit("notify", { message: "API Key 已撤销", type: "success" });
  } catch (error) {
    emit("notify", { message: error.message, type: "error" });
  }
}

async function copyKey() {
  await navigator.clipboard.writeText(newKey.value);
  emit("notify", { message: "密钥已复制", type: "success" });
}

defineExpose({ open, load });
</script>

<template>
  <dialog ref="dialog" class="key-dialog">
    <div class="key-dialog-inner">
      <div class="card-heading">
        <div><p class="eyebrow">DEVELOPER ACCESS</p><h3>API Key 管理</h3></div>
        <button class="dialog-close" type="button" aria-label="关闭" @click="close">×</button>
      </div>

      <div class="key-create-row">
        <input v-model="name" class="clean-input" placeholder="密钥名称，例如：研究脚本">
        <select v-model="expiresDays" class="clean-input">
          <option :value="30">30 天</option>
          <option :value="90">90 天</option>
          <option :value="365">1 年</option>
        </select>
        <button class="primary-button" type="button" :disabled="loading" @click="createKey">
          {{ loading ? "创建中" : "创建" }}
        </button>
      </div>

      <div v-if="newKey" class="new-key-reveal">
        <span>请立即复制，关闭后将不再显示</span>
        <code>{{ newKey }}</code>
        <button class="outline-button" type="button" @click="copyKey">复制密钥</button>
      </div>

      <div class="api-key-list">
        <div v-if="!keys.length" class="empty-state compact"><span>◇</span><p>还没有创建 API Key。</p></div>
        <div v-for="key in keys" v-else :key="key.id" class="api-key-item">
          <div><strong>{{ key.name }}</strong><span>{{ key.key_prefix }}…</span></div>
          <span>{{ key.expires_at ? `到期 ${key.expires_at.slice(0, 10)}` : "长期有效" }}</span>
          <em v-if="key.revoked_at">已撤销</em>
          <button v-else type="button" @click="revokeKey(key.id)">撤销</button>
        </div>
      </div>
    </div>
  </dialog>
</template>
