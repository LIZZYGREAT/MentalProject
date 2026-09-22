const state = { app: null, module: "A", scenarioId: null, packet: null, activeEvidenceSelect: null, adjudicationIndex: 0 };
const $ = (id) => document.getElementById(id);

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || JSON.stringify(body));
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

function scenarioItems() {
  const items = state.app.modules[state.module].items;
  return $("incomplete-only").checked ? items.filter(item => item.status !== "COMPLETE") : items;
}

function refreshScenarioSelect() {
  const select = $("scenario-select");
  const items = scenarioItems();
  select.innerHTML = items.map(item => `<option value="${item.scenario_id}">${item.scenario_id} · ${item.status}${item.flagged_for_review ? " ⚑" : ""}</option>`).join("");
  if (!items.length) return;
  if (!items.some(item => item.scenario_id === state.scenarioId)) state.scenarioId = items[0].scenario_id;
  select.value = state.scenarioId;
}

function renderContext(scenario) {
  $("scenario-context").innerHTML = `
    <div class="context-block"><h2>Focal Window</h2><p>${escapeHtml(scenario.focal_window.narrative)}</p><small>${escapeHtml(scenario.focal_window.start)} → ${escapeHtml(scenario.focal_window.end)}</small></div>
    <div class="context-block"><h2>Participant Context</h2><p>${escapeHtml(scenario.participant_context.text)}</p></div>
    <div class="context-block"><h2>Events / Tasks</h2><pre>${escapeHtml(JSON.stringify([...scenario.focal_events, ...scenario.current_tasks], null, 2))}</pre></div>`;
}

function evidenceOptions(selected = []) {
  return state.packet.evidence.map(item => `<option value="${escapeHtml(item.evidence_ref)}" ${selected.includes(item.evidence_ref) ? "selected" : ""}>${escapeHtml(item.evidence_ref)} · ${escapeHtml(item.speaker)}</option>`).join("");
}

function selected(value, expected) { return String(value ?? "") === String(expected) ? "selected" : ""; }

function localDateTime(value) {
  if (!value || ["UNKNOWN", "N/A"].includes(value)) return "";
  const date = new Date(value);
  const pad = number => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function labelControl(spec, value) {
  if (spec.kind === "select") return `<select class="label"><option value="">—</option>${spec.options.map(option => `<option value="${option}" ${String(value) === String(option) ? "selected" : ""}>${option}</option>`).join("")}</select>`;
  if (spec.kind === "datetime") return `<input class="label" type="datetime-local" value="${localDateTime(value)}"><select class="special"><option value="">datetime</option>${spec.special_options.map(option => `<option ${value === option ? "selected" : ""}>${option}</option>`).join("")}</select>`;
  if (spec.kind === "number") return `<input class="label" type="number" step="any" value="${typeof value === "number" ? value : ""}"><select class="special"><option value="">number</option>${spec.special_options.map(option => `<option ${value === option ? "selected" : ""}>${option}</option>`).join("")}</select>`;
  return `<input class="label" type="text" value="${escapeHtml(value || "")}" placeholder="${escapeHtml(spec.placeholder || "")}">`;
}

function renderRecordForms() {
  const saved = state.packet.draft?.payload?.records || [];
  const byKey = new Map(saved.map(record => [`${record.target_ref}::${record.variable}`, record]));
  const chunks = [];
  for (const target of state.packet.targets) {
    chunks.push(`<h2 class="target-heading">Target · ${escapeHtml(target)}</h2>`);
    for (const spec of state.packet.field_specs) {
      const record = byKey.get(`${target}::${spec.variable}`) || {};
      chunks.push(`<article class="record-card" data-target="${escapeHtml(target)}" data-variable="${spec.variable}">
        <div class="variable"><h3>${spec.variable}</h3><small>${spec.kind}</small></div>
        <label>Label ${labelControl(spec, record.label)}</label>
        <label>Evidence refs <select class="evidence-refs" multiple>${evidenceOptions(record.evidence_refs || [])}</select></label>
        <label class="full">Evidence span <textarea class="evidence-span" rows="2">${escapeHtml(record.evidence_span || "")}</textarea></label>
        <label>Evidence strength <select class="evidence-strength">${["STRONG", "MODERATE", "WEAK", "N/A"].map(value => `<option ${selected(record.evidence_strength || "STRONG", value)}>${value}</option>`).join("")}</select></label>
        <label>Confidence <select class="confidence">${["LOW", "MEDIUM", "HIGH"].map(value => `<option ${selected(record.annotator_confidence || "MEDIUM", value)}>${value}</option>`).join("")}</select></label>
        <label>Unknown reason <select class="unknown-reason"><option value="">—</option>${["NOT_MENTIONED", "INSUFFICIENT_DETAIL", "CONFLICTING_EVIDENCE", "TEMPORAL_SCOPE_UNCLEAR", "TARGET_UNCLEAR", "SOURCE_UNRELIABLE", "OTHER"].map(value => `<option ${selected(record.unknown_reason, value)}>${value}</option>`).join("")}</select></label>
        <label>Scope <select class="scope"><option value="">—</option>${["EPISODE", "EVENT_CLASS", "STABLE_GENERAL", "UNKNOWN_SCOPE"].map(value => `<option ${selected(record.scope, value)}>${value}</option>`).join("")}</select></label>
        <label>Partial basis <select class="partial-basis"><option value="">—</option>${["ACTUAL_INTERVAL", "FRACTION_ONLY", "OTHER_EXPLICIT"].map(value => `<option ${selected(record.partial_encoding_basis, value)}>${value}</option>`).join("")}</select></label>
        <label class="check"><input class="ambiguity" type="checkbox" ${record.ambiguity_flag ? "checked" : ""}> Ambiguity</label>
        <label class="full">Notes <textarea class="notes" rows="2">${escapeHtml(record.notes || "")}</textarea></label>
      </article>`);
    }
  }
  $("record-forms").innerHTML = chunks.join("");
  document.querySelectorAll(".record-card").forEach(card => card.addEventListener("click", () => {
    document.querySelectorAll(".record-card").forEach(item => item.classList.remove("active"));
    card.classList.add("active");
    state.activeEvidenceSelect = card.querySelector(".evidence-refs");
  }));
}

function renderEvidence() {
  $("evidence-list").innerHTML = state.packet.evidence.map(item => `<div class="evidence-card" data-ref="${escapeHtml(item.evidence_ref)}"><div class="evidence-meta"><strong>${escapeHtml(item.evidence_ref)}</strong><span>${escapeHtml(item.speaker)}</span><span>${escapeHtml(item.known_at)}</span></div><div>${escapeHtml(item.text)}</div></div>`).join("");
  document.querySelectorAll(".evidence-card").forEach(card => card.addEventListener("click", () => {
    if (!state.activeEvidenceSelect) return;
    const option = [...state.activeEvidenceSelect.options].find(item => item.value === card.dataset.ref);
    if (option) option.selected = true;
  }));
}

function restoreValidity() {
  const draft = state.packet.draft;
  const validity = draft?.payload?.scenario_validity || {};
  $("scenario-valid").value = validity.scenario_valid || "";
  $("scenario-plausibility").value = validity.scenario_plausibility || "";
  $("contradiction-present").value = validity.contradiction_present || "";
  $("flag-review").checked = Boolean(draft?.flagged_for_review);
}

async function loadScenario() {
  state.packet = await api(`/api/scenario/${state.module}/${state.scenarioId}`);
  $("current-id").textContent = `${state.scenarioId} · Module ${state.module}`;
  const moduleState = state.app.modules[state.module];
  $("progress").textContent = `${moduleState.complete} / ${moduleState.total}`;
  const status = state.packet.draft?.status || "NOT_STARTED";
  $("save-status").textContent = status;
  $("save-status").className = `status-pill ${status === "COMPLETE" ? "complete" : ""}`;
  renderContext(state.packet.scenario);
  renderEvidence();
  $("manual-excerpt").textContent = state.packet.manual_excerpt;
  restoreValidity();
  renderRecordForms();
}

function readLabel(card) {
  const special = card.querySelector(".special");
  if (special?.value) return special.value;
  const input = card.querySelector(".label");
  if (!input.value) return null;
  if (input.type === "number") return Number(input.value);
  if (input.type === "datetime-local") return new Date(input.value).toISOString();
  if (["0", "0.5", "1"].includes(input.value) && ["VALIDATION", "GUIDANCE", "RELEVANCE"].includes(card.dataset.variable)) return Number(input.value);
  return input.value;
}

function collectPayload(markComplete) {
  const records = [...document.querySelectorAll(".record-card")].map(card => {
    const record = {
      target_ref: card.dataset.target,
      variable: card.dataset.variable,
      label: readLabel(card),
      evidence_refs: [...card.querySelector(".evidence-refs").selectedOptions].map(item => item.value),
      evidence_strength: card.querySelector(".evidence-strength").value,
      ambiguity_flag: card.querySelector(".ambiguity").checked,
      annotator_confidence: card.querySelector(".confidence").value,
    };
    const optional = {
      evidence_span: card.querySelector(".evidence-span").value,
      unknown_reason: card.querySelector(".unknown-reason").value,
      scope: card.querySelector(".scope").value,
      partial_encoding_basis: card.querySelector(".partial-basis").value,
      notes: card.querySelector(".notes").value,
    };
    for (const [key, value] of Object.entries(optional)) if (value) record[key] = value;
    return record;
  }).filter(record => record.label !== null);
  return {
    scenario_validity: {
      scenario_valid: $("scenario-valid").value,
      scenario_plausibility: $("scenario-plausibility").value,
      contradiction_present: $("contradiction-present").value,
    },
    records,
    flagged_for_review: $("flag-review").checked,
    mark_complete: markComplete,
  };
}

async function save(markComplete, moveNext = false) {
  try {
    const draft = await api(`/api/draft/${state.module}/${state.scenarioId}`, { method: "POST", body: JSON.stringify(collectPayload(markComplete)) });
    $("save-status").textContent = draft.status;
    $("save-status").className = `status-pill ${draft.status === "COMPLETE" ? "complete" : draft.validation_errors.length ? "error" : ""}`;
    if (draft.validation_errors.length) alert(draft.validation_errors.join("\n"));
    state.app = await api("/api/state");
    refreshScenarioSelect();
    if (moveNext && draft.status === "COMPLETE") navigate(1);
  } catch (error) { alert(error.message); }
}

function navigate(delta) {
  const items = scenarioItems();
  const index = items.findIndex(item => item.scenario_id === state.scenarioId);
  state.scenarioId = items[(index + delta + items.length) % items.length].scenario_id;
  refreshScenarioSelect();
  loadScenario();
}

async function renderAdjudication() {
  $("annotation-view").classList.add("hidden");
  $("annotation-toolbar").classList.add("hidden");
  $("annotation-actions").classList.add("hidden");
  $("adjudication-view").classList.remove("hidden");
  if (!state.app.total) {
    $("adjudication-view").innerHTML = "<h2>Adjudication complete</h2><p>No disagreement items were produced by the completed analysis.</p>";
    return;
  }
  await loadAdjudicationItem();
}

async function loadAdjudicationItem() {
  const item = await api(`/api/adjudication/item/${state.adjudicationIndex}`);
  const queue = item.queue_item;
  const saved = item.saved_decision || {};
  $("current-id").textContent = `${queue.scenario_id} · ${queue.variable}`;
  $("progress").textContent = `${state.app.complete} / ${state.app.total}`;
  $("save-status").textContent = item.saved_decision ? "SAVED" : "PENDING";
  $("save-status").className = `status-pill ${item.saved_decision ? "complete" : ""}`;
  const labels = item.independent_labels.map(row => `<article class="decision-card"><strong>${escapeHtml(row.annotator_id)}</strong><div class="decision-label">${escapeHtml(row.label)}</div><div>${escapeHtml((row.evidence_refs || []).join(", "))}</div><blockquote>${escapeHtml(row.evidence_span || "No evidence span")}</blockquote></article>`).join("");
  const violations = item.critical_violations.length
    ? item.critical_violations.map(row => `<li>${escapeHtml(row.annotator_id)} · ${escapeHtml(row.violation_type)}</li>`).join("")
    : "<li>None for this field</li>";
  $("adjudication-view").innerHTML = `
    <section class="adjudication-summary">
      <div><p class="eyebrow">${escapeHtml(queue.severity)} disagreement</p><h2>${escapeHtml(queue.target_ref)} · ${escapeHtml(queue.variable)}</h2><p>${escapeHtml(queue.suspected_cause)}</p></div>
      <div class="adjudication-nav"><button id="adj-previous">Previous</button><span>${state.adjudicationIndex + 1} / ${state.app.total}</span><button id="adj-next">Next</button></div>
    </section>
    <section class="adjudication-grid">
      <div>
        <article class="context-block"><h2>Scenario</h2><p>${escapeHtml(item.scenario.focal_window.narrative)}</p><pre>${escapeHtml(JSON.stringify(item.scenario, null, 2))}</pre></article>
        <details class="manual-card"><summary>Coding Manual</summary><pre>${escapeHtml(item.manual)}</pre></details>
      </div>
      <div>
        <h2>Independent labels</h2><div class="decision-list">${labels}</div>
        <article class="context-block"><h2>Agreement</h2><pre>${escapeHtml(JSON.stringify(item.agreement.label_counts, null, 2))}</pre><h2>Critical violation flags</h2><ul>${violations}</ul></article>
        <form id="adjudication-form" class="adjudication-form">
          <label>Final label <input id="final-label" required value="${escapeHtml(saved.gold_label ?? "")}"></label>
          <label>Decision <select id="decision" required><option value="">—</option>${["KEEP", "REVISE", "SIMPLIFY", "DROP"].map(value => `<option ${selected(saved.decision, value)}>${value}</option>`).join("")}</select></label>
          <label>Reviewer <input id="adjudicated-by" required value="${escapeHtml((saved.adjudicated_by || []).join(", "))}" placeholder="name or reviewer id"></label>
          <label class="full">Adjudication reason <textarea id="adjudication-reason" required rows="5">${escapeHtml(saved.adjudication_reason || "")}</textarea></label>
          <button class="primary" type="submit">Save adjudication</button>
        </form>
      </div>
    </section>`;
  $("adj-previous").addEventListener("click", () => navigateAdjudication(-1));
  $("adj-next").addEventListener("click", () => navigateAdjudication(1));
  $("adjudication-form").addEventListener("submit", saveAdjudication);
}

function navigateAdjudication(delta) {
  state.adjudicationIndex = (state.adjudicationIndex + delta + state.app.total) % state.app.total;
  loadAdjudicationItem().catch(error => alert(error.message));
}

async function saveAdjudication(event) {
  event.preventDefault();
  try {
    await api(`/api/adjudication/item/${state.adjudicationIndex}`, {
      method: "POST",
      body: JSON.stringify({
        final_label: $("final-label").value,
        decision: $("decision").value,
        adjudication_reason: $("adjudication-reason").value,
        adjudicated_by: $("adjudicated-by").value.split(",").map(value => value.trim()).filter(Boolean),
      }),
    });
    state.app = await api("/api/state");
    await loadAdjudicationItem();
  } catch (error) { alert(error.message); }
}

async function init() {
  state.app = await api("/api/state");
  if (state.app.mode === "adjudication") return renderAdjudication();
  state.module = $("module-select").value;
  refreshScenarioSelect();
  state.scenarioId = $("scenario-select").value;
  await loadScenario();
  $("module-select").addEventListener("change", async event => { state.module = event.target.value; state.scenarioId = null; refreshScenarioSelect(); state.scenarioId = $("scenario-select").value; await loadScenario(); });
  $("scenario-select").addEventListener("change", async event => { state.scenarioId = event.target.value; await loadScenario(); });
  $("incomplete-only").addEventListener("change", async () => { refreshScenarioSelect(); state.scenarioId = $("scenario-select").value; if (state.scenarioId) await loadScenario(); });
  $("previous").addEventListener("click", () => navigate(-1));
  $("next").addEventListener("click", () => navigate(1));
  $("save-draft").addEventListener("click", () => save(false));
  $("save-next").addEventListener("click", () => save(true, true));
}

init().catch(error => { document.body.innerHTML = `<pre>${escapeHtml(error.message)}</pre>`; });
