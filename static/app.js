(() => {
    const state = {
        dashboard: null,
        questionnaire: null,
        questionnaireStep: 0,
        answers: {},
        mockEvents: [],
        feedbackPeriod: "morning",
        latestRunId: null,
        apiKeys: []
    };

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

    function escapeHtml(value) {
        return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    async function api(url, options = {}) {
        const response = await fetch(url, {
            ...options,
            headers: {
                ...(options.body ? {"Content-Type": "application/json"} : {}),
                ...(options.headers || {})
            }
        });
        if (response.status === 401) {
            window.location.assign("/login");
            throw new Error("登录已过期");
        }
        let payload = {};
        try { payload = await response.json(); } catch (_) {}
        if (!response.ok) {
            throw new Error(payload.message || "请求未完成，请稍后重试");
        }
        return payload;
    }

    function toast(message, type = "success") {
        const item = document.createElement("div");
        item.className = `toast ${type === "error" ? "error" : ""}`;
        item.textContent = message;
        $("#toastStack").append(item);
        window.setTimeout(() => item.remove(), 3600);
    }

    function setView(view) {
        $$(".view").forEach(panel => panel.classList.toggle("active", panel.dataset.viewPanel === view));
        $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
        const labels = {
            dashboard: "今日概览",
            profile: "我的画像",
            prediction: "趋势预测",
            feedback: "轻量反馈",
            settings: "设置"
        };
        $("#pageTitle").textContent = labels[view] || "心序";
        $("#sidebar").classList.remove("open");
        window.scrollTo({top: 0, behavior: "smooth"});
        if (view === "settings") loadConnections();
    }

    function localDateLabel() {
        const now = new Date();
        const weekdays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
        return `${now.getMonth() + 1}月${now.getDate()}日 · ${weekdays[now.getDay()]}`;
    }

    function timeGreeting() {
        const hour = new Date().getHours();
        if (hour < 11) return "早上好";
        if (hour < 14) return "中午好";
        if (hour < 18) return "下午好";
        return "晚上好";
    }

    function renderDashboard(data) {
        state.dashboard = data;
        state.latestRunId = data.recent_runs?.[0]?.prediction_run_id || null;
        $("#todayDate").textContent = localDateLabel();
        const welcome = $(".welcome-row h2");
        if (welcome) {
            welcome.firstChild.textContent = `${timeGreeting()}，`;
        }
        $("#onboardingBanner").hidden = data.onboarding_completed;
        $("#profileStatusLabel").textContent = data.onboarding_completed ? "已建立" : "待初始化";
        $("#profileStatusDescription").textContent = data.onboarding_completed
            ? "初始画像已就绪，可查看推断依据"
            : "完成问卷后生成可审计画像";

        const versions = data.versions || {};
        $("#modelVersionChip").textContent = versions.model || "基线模型";
        $("#settingsModelVersion").textContent = versions.model || "—";
        $("#settingsParameterVersion").textContent = versions.parameters || "—";
        $("#settingsFeatureVersion").textContent = versions.features || "—";

        renderProfile(data.profile);
        renderRoutine(data.routine_plan);
        renderRecentRuns(data.recent_runs || []);
        renderLatestTrend(data.recent_runs?.[0]);
    }

    function renderLatestTrend(run) {
        if (!run) return;
        const result = run.result || {};
        const endS = Number(result.end_S ?? 0);
        const endE = Number(result.end_E ?? 0);
        $("#trendLabel").textContent = endS >= 75 ? "负荷偏高" : endS >= 55 ? "温和上升" : "相对平稳";
        $("#trendDescription").textContent = `最近一次日终参考 ${endS.toFixed(0)} / 100`;
        $("#trendMeter").style.width = `${Math.max(8, Math.min(100, endS))}%`;
        $("#energyLabel").textContent = endE < 30 ? "恢复空间较少" : endE < 60 ? "需要留意" : "相对充足";
        $("#energyDescription").textContent = `最近一次精力参考 ${endE.toFixed(0)} / 100`;
        $("#energyMeter").style.width = `${Math.max(8, Math.min(100, endE))}%`;
    }

    function renderRoutine(plan) {
        const container = $("#routineTimeline");
        if (!plan?.items?.length) {
            container.innerHTML = `
                <div class="empty-state compact">
                    <span>○</span>
                    <p>完成问卷后，这里会显示午餐、午睡与晚餐的建议时间。</p>
                </div>`;
            return;
        }
        const labels = {lunch: "午餐", nap: "午间休息", dinner: "晚餐"};
        container.innerHTML = `<div class="routine-items">${
            plan.items.map(item => {
                const unavailable = !item.scheduled_window;
                const time = unavailable ? "暂未安排" : item.scheduled_window.join("–");
                const status = {
                    scheduled: "理想时间",
                    shifted: "已避开日程",
                    shortened: "已缩短",
                    unavailable: "时间冲突",
                    not_expected: "按你的习惯"
                }[item.status] || item.status;
                return `<div class="routine-item ${unavailable ? "unavailable" : ""}">
                    <time>${escapeHtml(time)}</time>
                    <div><strong>${escapeHtml(labels[item.routine_type] || item.routine_type)}</strong>
                    <small>${unavailable ? "不会自动创建恢复事件" : "来自问卷作息偏好"}</small></div>
                    <em>${escapeHtml(status)}</em>
                </div>`;
            }).join("")
        }</div>`;
    }

    function renderRecentRuns(runs) {
        const container = $("#recentRuns");
        if (!runs.length) {
            container.innerHTML = `
                <div class="empty-state">
                    <span>⌁</span><h4>还没有趋势记录</h4>
                    <p>运行一次今日预测后，结果会以可重放版本保存在这里。</p>
                </div>`;
            return;
        }
        container.innerHTML = runs.map(run => {
            const result = run.result || {};
            return `<div class="recent-run-item">
                <time>${escapeHtml(run.local_date)}</time>
                <strong>${escapeHtml(run.model_version)}</strong>
                <span>压力 ${Number(result.end_S ?? 0).toFixed(0)}</span>
                <span>精力 ${Number(result.end_E ?? 0).toFixed(0)}</span>
                <span>${(result.alerts || []).length} 条提示</span>
            </div>`;
        }).join("");
    }

    function renderProfile(profile) {
        $("#profileEmpty").hidden = Boolean(profile);
        $("#profileContent").hidden = !profile;
        if (!profile) return;
        $("#profileSummary").textContent = profile.summary || "画像已建立。";
        $("#mappingVersion").textContent = profile.mapping_version || "v1";
        $("#traitGrid").innerHTML = (profile.traits || []).map(trait => {
            const score = Math.round(Number(trait.score_0_1 || 0) * 100);
            const confidence = Math.round(Number(trait.confidence_0_1 || 0) * 100);
            const evidence = (trait.evidence || []).map(item =>
                `<li>${escapeHtml(item.question_id)} · ${escapeHtml(item.role)}</li>`
            ).join("");
            return `<article class="trait-card">
                <div class="trait-score" style="--score:${score}%"><b>${score}</b></div>
                <h4>${escapeHtml(trait.label || trait.trait)}</h4>
                <p>推断置信度 ${confidence}% · 分数仅作为模型先验</p>
                <details><summary>查看依据</summary><ul>${evidence}</ul></details>
            </article>`;
        }).join("");
        $("#priorList").innerHTML = (profile.parameter_priors || []).map(prior => `
            <div class="prior-item">
                <strong>${escapeHtml(prior.parameter)}</strong>
                <b>${Number(prior.mean).toFixed(2)}</b>
                <div class="prior-range" title="${escapeHtml(prior.lower)} – ${escapeHtml(prior.upper)}"><span></span></div>
            </div>`).join("");
    }

    function questionnaireSections() {
        return state.questionnaire?.sections || [];
    }

    async function openOnboarding() {
        try {
            if (!state.questionnaire) {
                const payload = await api("/api/onboarding/questionnaire");
                state.questionnaire = payload.questionnaire;
            }
            const existing = state.dashboard?.profile;
            state.answers = existing ? {
                ...(existing.routine || {}),
                support_style: existing.care_preferences?.preferred_support || [],
                care_tone: existing.care_preferences?.tone || "brief_warm"
            } : {};
            state.questionnaireStep = 0;
            renderQuestionnaireStep();
            $("#onboardingDialog").showModal();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    function renderQuestionnaireStep() {
        const sections = questionnaireSections();
        const section = sections[state.questionnaireStep];
        if (!section) return;
        $("#questionProgressLabel").textContent = `第 ${state.questionnaireStep + 1} / ${sections.length} 步`;
        $("#questionProgressBar").style.width = `${((state.questionnaireStep + 1) / sections.length) * 100}%`;
        $("#previousQuestionButton").hidden = state.questionnaireStep === 0;
        $("#nextQuestionButton").hidden = state.questionnaireStep === sections.length - 1;
        $("#submitQuestionnaireButton").hidden = state.questionnaireStep !== sections.length - 1;

        const timeQuestions = section.questions.filter(q => q.response_type === "local_time");
        const otherQuestions = section.questions.filter(q => q.response_type !== "local_time");
        const timeMarkup = timeQuestions.length
            ? `<div class="time-question-grid">${timeQuestions.map(renderQuestion).join("")}</div>`
            : "";
        $("#questionContainer").innerHTML = `
            <div class="section-heading">
                <p class="eyebrow">${escapeHtml(section.eyebrow)}</p>
                <h2>${escapeHtml(section.title)}</h2>
                <p>${escapeHtml(section.description)}</p>
            </div>
            <div id="questionError" class="question-error" hidden></div>
            ${timeMarkup}
            ${otherQuestions.map(renderQuestion).join("")}`;
    }

    function renderQuestion(question) {
        const value = state.answers[question.question_id] ?? question.default ?? "";
        const required = question.required ? "required" : "";
        if (question.response_type === "local_time") {
            return `<div class="question-field">
                <label for="q_${escapeHtml(question.question_id)}">${escapeHtml(question.prompt)}</label>
                <input class="clean-input" id="q_${escapeHtml(question.question_id)}"
                       name="${escapeHtml(question.question_id)}" type="time"
                       value="${escapeHtml(value)}" ${required}>
            </div>`;
        }
        if (question.response_type === "likert_1_5") {
            return `<div class="question-field">
                <fieldset><legend>${escapeHtml(question.prompt)}</legend>
                    <div class="likert-options">${[1,2,3,4,5].map(score => `
                        <label class="likert-option">
                            <input type="radio" name="${escapeHtml(question.question_id)}"
                                   value="${score}" ${Number(value) === score ? "checked" : ""} ${required}>
                            <span>${score}</span>
                        </label>`).join("")}</div>
                    <div class="likert-labels"><span>完全不符合</span><span>非常符合</span></div>
                </fieldset>
            </div>`;
        }
        if (question.response_type === "single_choice" || question.response_type === "multiple_choice") {
            const values = Array.isArray(value) ? value : [value];
            const inputType = question.response_type === "multiple_choice" ? "checkbox" : "radio";
            return `<div class="question-field">
                <fieldset><legend>${escapeHtml(question.prompt)}</legend>
                    <div class="choice-options">${question.options.map(option => `
                        <label class="choice-option">
                            <input type="${inputType}" name="${escapeHtml(question.question_id)}"
                                   value="${escapeHtml(option.value)}"
                                   ${values.includes(option.value) ? "checked" : ""} ${required}>
                            <span>${escapeHtml(option.label)}</span>
                        </label>`).join("")}</div>
                </fieldset>
            </div>`;
        }
        return `<div class="question-field">
            <label for="q_${escapeHtml(question.question_id)}">${escapeHtml(question.prompt)}</label>
            <textarea class="clean-input" id="q_${escapeHtml(question.question_id)}"
                      name="${escapeHtml(question.question_id)}" rows="3"
                      placeholder="${escapeHtml(question.help || "")}">${escapeHtml(value)}</textarea>
            ${question.help ? `<small class="question-help">${escapeHtml(question.help)}</small>` : ""}
        </div>`;
    }

    function collectCurrentAnswers() {
        const section = questionnaireSections()[state.questionnaireStep];
        const error = $("#questionError");
        const missing = [];
        for (const question of section.questions) {
            const controls = $$(`[name="${CSS.escape(question.question_id)}"]`, $("#questionContainer"));
            let value;
            if (question.response_type === "likert_1_5" || question.response_type === "single_choice") {
                value = controls.find(control => control.checked)?.value;
                if (question.response_type === "likert_1_5" && value) value = Number(value);
            } else if (question.response_type === "multiple_choice") {
                value = controls.filter(control => control.checked).map(control => control.value);
            } else {
                value = controls[0]?.value?.trim();
            }
            if (question.required && (value === undefined || value === "" || value.length === 0)) {
                missing.push(question.prompt);
            } else {
                state.answers[question.question_id] = value ?? "";
            }
        }
        if (missing.length) {
            error.textContent = `还有 ${missing.length} 项没有回答，请完成后继续。`;
            error.hidden = false;
            return false;
        }
        error.hidden = true;
        return true;
    }

    async function submitQuestionnaire() {
        if (!collectCurrentAnswers()) return;
        const button = $("#submitQuestionnaireButton");
        button.disabled = true;
        button.firstChild.textContent = "正在生成画像 ";
        try {
            const payload = await api("/api/onboarding/responses", {
                method: "POST",
                body: JSON.stringify({
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
                    answers: state.answers
                })
            });
            $("#onboardingDialog").close();
            toast("画像已建立。每一项推断都可以查看依据。");
            const dashboard = await api("/api/dashboard");
            renderDashboard(dashboard);
            setView("profile");
        } catch (error) {
            toast(error.message, "error");
        } finally {
            button.disabled = false;
            button.firstChild.textContent = "生成我的画像 ";
        }
    }

    function renderEvents() {
        const list = $("#eventList");
        list.innerHTML = state.mockEvents.map((event, index) => `
            <div class="event-chip">
                <div><strong>${escapeHtml(event.name)}</strong>
                <span>${escapeHtml(event.start)}–${escapeHtml(event.end)} · ${escapeHtml(event.type)}</span></div>
                <button data-remove-event="${index}" type="button" aria-label="移除">×</button>
            </div>`).join("");
    }

    function addEvent() {
        const name = $("#eventName").value.trim();
        const start = $("#eventStart").value;
        const end = $("#eventEnd").value;
        if (!name || !start || !end) return toast("请填写日程名称与时间", "error");
        if (start >= end) return toast("结束时间需要晚于开始时间", "error");
        const overlap = state.mockEvents.some(item => start < item.end && end > item.start);
        if (overlap) return toast("该时段与已添加日程重叠", "error");
        state.mockEvents.push({
            name,
            start,
            end,
            type: $("#eventType").value,
            level: $("#eventLevel").value,
            credit: 2,
            hours: 32,
            intensity: 0.7,
            study_intensity: 0.7
        });
        $("#eventName").value = "";
        renderEvents();
    }

    async function runPrediction() {
        const button = $("#runPredictionButton");
        button.disabled = true;
        button.querySelector("span").textContent = "正在生成趋势…";
        $("#chartArea").innerHTML = `<div class="empty-state"><span>∿</span><h4>正在整理日程与节律</h4><p>首次运行可能需要几秒。</p></div>`;
        try {
            const payload = await api("/api/simulate", {
                method: "POST",
                body: JSON.stringify({
                    date: $("#simDate").value,
                    init_S: Number($("#initS").value),
                    init_E: Number($("#initE").value),
                    mock_events: state.mockEvents,
                    shield_keywords: [],
                    shield_time_ranges: []
                })
            });
            state.latestRunId = payload.prediction_run_id;
            $("#chartArea").innerHTML = payload.image
                ? `<img src="data:image/png;base64,${payload.image}" alt="当天压力与精力趋势图">`
                : `<div class="empty-state"><span>○</span><p>本次未生成图像，但结构化结果已保存。</p></div>`;
            $("#resultMetrics").hidden = false;
            $("#endS").textContent = Number(payload.end_S).toFixed(1);
            $("#endE").textContent = Number(payload.end_E).toFixed(1);
            $("#alertCount").textContent = `${(payload.alerts || []).length} 条`;
            $("#runFingerprint").textContent = `指纹 ${payload.input_fingerprint.slice(0, 10)}`;
            $("#detailModel").textContent = payload.versions.model;
            $("#detailParams").textContent = payload.versions.parameters;
            $("#detailFeatures").textContent = payload.versions.features;
            $("#detailRun").textContent = payload.prediction_run_id;
            renderAlerts(payload.alerts || []);
            if (payload.routine_plan) renderRoutine(payload.routine_plan);
            toast("趋势已生成并保存为可重放运行");
            const dashboard = await api("/api/dashboard");
            renderDashboard(dashboard);
        } catch (error) {
            $("#chartArea").innerHTML = `<div class="empty-state"><span>!</span><h4>暂时无法生成</h4><p>${escapeHtml(error.message)}</p></div>`;
            toast(error.message, "error");
        } finally {
            button.disabled = false;
            button.querySelector("span").textContent = "生成今日趋势";
        }
    }

    function renderAlerts(alerts) {
        $("#alertsCard").hidden = alerts.length === 0;
        $("#alertList").innerHTML = alerts.map(alert => `
            <div class="alert-item">
                <strong>${escapeHtml(alert.type || "趋势提示")}</strong>
                <p>${escapeHtml(alert.message || alert.reason || JSON.stringify(alert))}</p>
            </div>`).join("");
    }

    async function submitFeedback() {
        const button = $("#submitFeedbackButton");
        button.disabled = true;
        try {
            await api("/api/feedback", {
                method: "POST",
                body: JSON.stringify({
                    feedback_type: "momentary_state",
                    prediction_run_id: state.latestRunId,
                    target_time: new Date().toISOString(),
                    retrospective: false,
                    payload: {
                        period: state.feedbackPeriod,
                        stress_0_10: Number($("#feedbackStress").value),
                        energy_0_10: Number($("#feedbackEnergy").value),
                        note: $("#feedbackNote").value.trim()
                    }
                })
            });
            $("#feedbackNote").value = "";
            toast("已保存此刻感受，谢谢你的记录");
        } catch (error) {
            toast(error.message, "error");
        } finally {
            button.disabled = false;
        }
    }

    function updateReviewForm() {
        const labels = {
            peak_review: "实际峰值（0–10）",
            event_impact: "实际影响（0–10）",
            prediction_review: "预警准确度（0–10）",
            care_review: "帮助程度（0–10）",
            routine_correction: "与计划符合度（0–10）"
        };
        $("#reviewScoreLabel").textContent = labels[$("#reviewType").value];
    }

    async function submitReview() {
        const reviewType = $("#reviewType").value;
        const score = Number($("#reviewScore").value);
        if (!Number.isFinite(score) || score < 0 || score > 10) {
            return toast("复盘评分需要在 0–10 之间", "error");
        }
        const button = $("#submitReviewButton");
        button.disabled = true;
        try {
            await api("/api/feedback", {
                method: "POST",
                body: JSON.stringify({
                    feedback_type: reviewType,
                    prediction_run_id: state.latestRunId,
                    target_time: $("#reviewTime").value || null,
                    retrospective: true,
                    payload: {
                        score_0_10: score,
                        note_or_correction: $("#reviewNote").value.trim()
                    }
                })
            });
            $("#reviewNote").value = "";
            toast("复盘已保存，并与最近一次预测版本关联");
        } catch (error) {
            toast(error.message, "error");
        } finally {
            button.disabled = false;
        }
    }

    async function loadConnections() {
        try {
            const [token, keys] = await Promise.all([
                api("/api/token_status"),
                api("/api/auth/api-keys")
            ]);
            $("#tokenStatus").textContent = token.valid ? "已连接当前账号" : "尚未连接";
            $("#tokenStatus").className = `status-pill ${token.valid ? "safe" : "warning"}`;
            state.apiKeys = keys.api_keys || [];
            const active = state.apiKeys.filter(key => !key.revoked_at).length;
            $("#apiKeyCount").textContent = `${active} 个有效密钥`;
            renderApiKeys();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    function renderApiKeys() {
        const container = $("#apiKeyList");
        if (!state.apiKeys.length) {
            container.innerHTML = `<div class="empty-state compact"><span>◇</span><p>还没有创建 API Key。</p></div>`;
            return;
        }
        container.innerHTML = state.apiKeys.map(key => `
            <div class="api-key-item">
                <div><strong>${escapeHtml(key.name)}</strong><span>${escapeHtml(key.key_prefix)}…</span></div>
                <span>${key.expires_at ? `到期 ${escapeHtml(key.expires_at.slice(0, 10))}` : "长期有效"}</span>
                ${key.revoked_at ? "<em>已撤销</em>" : `<button data-revoke-key="${key.id}" type="button">撤销</button>`}
            </div>`).join("");
    }

    async function createApiKey() {
        const name = $("#keyName").value.trim();
        if (!name) return toast("请填写密钥名称", "error");
        try {
            const payload = await api("/api/auth/api-keys", {
                method: "POST",
                body: JSON.stringify({name, expires_days: Number($("#keyExpiry").value)})
            });
            $("#newKeyValue").textContent = payload.api_key.key;
            $("#newKeyReveal").hidden = false;
            $("#keyName").value = "";
            await loadConnections();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    async function revokeApiKey(id) {
        try {
            await api(`/api/auth/api-keys/${id}`, {method: "DELETE"});
            toast("API Key 已撤销");
            await loadConnections();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    async function connectFeishu() {
        try {
            const payload = await api("/api/feishu/get_url");
            if (payload.missing?.length) {
                toast(`服务端还未配置：${payload.missing.join("、")}`, "error");
                return;
            }
            window.open(payload.url, "_blank", "noopener,noreferrer");
        } catch (error) {
            toast(error.message, "error");
        }
    }

    function bindEvents() {
        $$(".nav-item").forEach(item => item.addEventListener("click", () => setView(item.dataset.view)));
        $$("[data-view-jump]").forEach(item => item.addEventListener("click", () => setView(item.dataset.viewJump)));
        $$("[data-view-link]").forEach(item => item.addEventListener("click", event => {
            event.preventDefault();
            setView(item.dataset.viewLink);
        }));
        $$("[data-open-profile]").forEach(item => item.addEventListener("click", () => setView("profile")));
        $$("[data-start-onboarding], #startOnboardingButton").forEach(item => item.addEventListener("click", openOnboarding));
        $("#mobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
        $("#logoutBtn").addEventListener("click", async () => {
            await api("/api/auth/logout", {method: "POST"});
            window.location.assign("/login");
        });
        $("#quickFeedbackButton").addEventListener("click", () => setView("feedback"));

        $("#closeOnboardingButton").addEventListener("click", () => $("#onboardingDialog").close());
        $("#previousQuestionButton").addEventListener("click", () => {
            if (!collectCurrentAnswers()) return;
            state.questionnaireStep -= 1;
            renderQuestionnaireStep();
        });
        $("#nextQuestionButton").addEventListener("click", () => {
            if (!collectCurrentAnswers()) return;
            state.questionnaireStep += 1;
            renderQuestionnaireStep();
        });
        $("#submitQuestionnaireButton").addEventListener("click", submitQuestionnaire);

        ["initS", "initE", "feedbackStress", "feedbackEnergy"].forEach(id => {
            const input = $(`#${id}`);
            const output = $(`#${id}Value`);
            input.addEventListener("input", () => output.textContent = input.value);
        });
        $("#simDate").value = new Date().toLocaleDateString("en-CA");
        $("#addEventButton").addEventListener("click", addEvent);
        $("#eventList").addEventListener("click", event => {
            const button = event.target.closest("[data-remove-event]");
            if (!button) return;
            state.mockEvents.splice(Number(button.dataset.removeEvent), 1);
            renderEvents();
        });
        $("#runPredictionButton").addEventListener("click", runPrediction);

        $$(".feedback-period button").forEach(button => button.addEventListener("click", () => {
            state.feedbackPeriod = button.dataset.period;
            $$(".feedback-period button").forEach(item => item.classList.toggle("active", item === button));
        }));
        $("#submitFeedbackButton").addEventListener("click", submitFeedback);
        $("#reviewType").addEventListener("change", updateReviewForm);
        $("#submitReviewButton").addEventListener("click", submitReview);
        $("#reviewTime").value = new Date().toTimeString().slice(0, 5);

        $("#connectFeishuButton").addEventListener("click", connectFeishu);
        $("#manageKeysButton").addEventListener("click", () => {
            $("#keyDialog").showModal();
            loadConnections();
        });
        $("#closeKeyDialog").addEventListener("click", () => $("#keyDialog").close());
        $("#createKeyButton").addEventListener("click", createApiKey);
        $("#copyKeyButton").addEventListener("click", async () => {
            await navigator.clipboard.writeText($("#newKeyValue").textContent);
            toast("密钥已复制");
        });
        $("#apiKeyList").addEventListener("click", event => {
            const button = event.target.closest("[data-revoke-key]");
            if (button) revokeApiKey(button.dataset.revokeKey);
        });
    }

    async function init() {
        bindEvents();
        try {
            const dashboard = await api("/api/dashboard");
            renderDashboard(dashboard);
        } catch (error) {
            toast(error.message, "error");
        } finally {
            $("#pageLoading").classList.add("hidden");
            window.setTimeout(() => $("#pageLoading").remove(), 300);
        }
    }

    init();
})();
