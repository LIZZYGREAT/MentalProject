const WIDTH = 1200;
const LEFT = 78;
const RIGHT = 1168;
const PLOT_WIDTH = RIGHT - LEFT;
const LAST_CURVE_MINUTE = 23 * 60 + 55;

const clamp = (value, minimum, maximum) =>
  Math.max(minimum, Math.min(maximum, Number(value)));

const escapeHtml = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]),
);

export function clockMinute(value) {
  const text = String(value ?? "").trim();
  const match = /(?:T|\s)?(\d{2}):(\d{2})/.exec(text);
  if (!match) return null;
  const minute = Number(match[1]) * 60 + Number(match[2]);
  return minute >= 0 && minute < 1440 ? minute : null;
}

function instantMinute(value, timezone) {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return clockMinute(value);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone || "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(parsed);
  const get = (type) => Number(parts.find((part) => part.type === type)?.value || 0);
  return get("hour") * 60 + get("minute");
}

const xForMinute = (minute) =>
  LEFT + clamp(minute, 0, LAST_CURVE_MINUTE) * PLOT_WIDTH / LAST_CURVE_MINUTE;

function yForValue(value, top, height, maximum = 10) {
  return top + height - clamp(value, 0, maximum) * height / maximum;
}

export function normalizedCurve(points) {
  return (Array.isArray(points) ? points : [])
    .map((point) => ({ ...point, minute: clockMinute(point.time) }))
    .filter((point) => point.minute !== null && Number.isFinite(Number(point.stress_0_10)))
    .sort((left, right) => left.minute - right.minute);
}

export function linePath(points, key, top, height, maximum = 10) {
  return points
    .filter((point) => Number.isFinite(Number(point[key])))
    .map((point, index) => {
      const command = index ? "L" : "M";
      return `${command}${xForMinute(point.minute).toFixed(1)},${yForValue(
        point[key], top, height, maximum,
      ).toFixed(1)}`;
    })
    .join(" ");
}

export function significantChanges(points) {
  const candidates = [];
  for (let index = 1; index < points.length; index += 1) {
    const previous = Number(points[index - 1].stress_0_10);
    const current = Number(points[index].stress_0_10);
    const delta = current - previous;
    if (!Number.isFinite(delta)) continue;
    candidates.push({
      minute: points[index].minute,
      time: points[index].time,
      delta,
      value: current,
      index,
    });
  }
  return candidates
    .sort((left, right) => Math.abs(right.delta) - Math.abs(left.delta))
    .filter((item, index, all) =>
      all.slice(0, index).every((selected) => Math.abs(selected.index - item.index) > 5),
    )
    .slice(0, 3)
    .sort((left, right) => left.minute - right.minute);
}

function grid(top, height, label) {
  return [0, 2, 4, 6, 8, 10].map((value) => {
    const y = yForValue(value, top, height);
    return `<line class="forecast-grid" x1="${LEFT}" y1="${y}" x2="${RIGHT}" y2="${y}"/>
      <text class="forecast-axis-label" x="${LEFT - 18}" y="${y + 4}" text-anchor="end">${value}</text>`;
  }).join("") + `<text class="forecast-axis-title" x="${LEFT}" y="${top - 16}">${label}</text>`;
}

function timeAxis() {
  const ticks = [0, 240, 480, 720, 960, 1200, LAST_CURVE_MINUTE];
  return ticks.map((minute, index) => {
    const label = minute === LAST_CURVE_MINUTE
      ? "23:55"
      : `${String(Math.floor(minute / 60)).padStart(2, "0")}:00`;
    const anchor = index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle";
    return `<line class="forecast-tick" x1="${xForMinute(minute)}" y1="596" x2="${xForMinute(minute)}" y2="602"/>
      <text class="forecast-axis-label" x="${xForMinute(minute)}" y="624" text-anchor="${anchor}">${label}</text>`;
  }).join("");
}

function calendarBands(data) {
  const events = data?.analysis?.important_calendar_events || data?.events || [];
  return events.slice(0, 16).map((event, index) => {
    const start = Number.isFinite(Number(event.minute))
      ? Number(event.minute)
      : clockMinute(event.start_time || event.time);
    const end = Number.isFinite(Number(event.end_minute))
      ? Number(event.end_minute)
      : clockMinute(event.end_time);
    if (start === null) return "";
    const safeEnd = Math.max(start + 5, end ?? start + 30);
    const x = xForMinute(start);
    const width = Math.max(3, xForMinute(safeEnd) - x);
    const label = String(event.summary || "日程").slice(0, 16);
    return `<g class="calendar-band calendar-band-${index % 4}">
      <rect x="${x}" y="64" width="${width}" height="526" rx="3"><title>${escapeHtml(label)} · ${escapeHtml(event.time || event.start_time || "")}</title></rect>
      ${width > 42 ? `<text x="${x + 5}" y="82">${escapeHtml(label)}</text>` : ""}
    </g>`;
  }).join("");
}

function warningMarkers(data) {
  return (data?.warnings || []).slice(0, 12).map((warning) => {
    const minute = clockMinute(
      warning.risk_time_local || warning.risk_time || warning.target_time_local || warning.target_time,
    );
    if (minute === null) return "";
    const x = xForMinute(minute);
    const label = warning.warning_level ? `${warning.warning_level}级风险` : "风险窗口";
    return `<g class="warning-marker"><line x1="${x}" y1="64" x2="${x}" y2="590"/>
      <circle cx="${x}" cy="64" r="5"><title>${escapeHtml(label)} · ${escapeHtml(warning.risk_time_local || "")}</title></circle></g>`;
  }).join("");
}

function observationMarkers(data, timezone, top, height, key, className) {
  return (data?.instant_observations || []).map((observation) => {
    const value = observation?.payload?.[key];
    const minute = instantMinute(observation.observed_at, timezone);
    if (minute === null || !Number.isFinite(Number(value))) return "";
    const x = xForMinute(minute);
    const y = yForValue(value, top, height);
    return `<circle class="${className}" cx="${x}" cy="${y}" r="5"><title>${escapeHtml(
      observation.observed_at,
    )} · ${Number(value).toFixed(1)}/10</title></circle>`;
  }).join("");
}

function reviewMarkers(data, top, height, key) {
  const retrospective = data?.retrospective;
  const responses = data?.daily_review_responses || [];
  const review = responses.find((item) => item.id === retrospective?.daily_review_response_id) || responses[0];
  if (!review) return "";
  const points = key === "stress_0_10"
    ? [[480, review.start_stress, "早晨回顾"], [clockMinute(retrospective?.diagnostics?.end_anchor_time), review.end_stress, "收尾回顾"]]
    : [[480, review.start_energy, "早晨回顾"], [clockMinute(retrospective?.diagnostics?.end_anchor_time), review.end_energy, "收尾回顾"]];
  return points.map(([minute, value, label]) => {
    if (minute === null || !Number.isFinite(Number(value))) return "";
    return `<rect class="review-marker" x="${xForMinute(minute) - 4}" y="${yForValue(value, top, height) - 4}" width="8" height="8"><title>${label} · ${Number(value).toFixed(1)}/10</title></rect>`;
  }).join("");
}

export function renderForecastChart(data, options = {}) {
  const points = normalizedCurve(data?.curve);
  if (!points.length) return '<div class="empty">所选日期没有可展示的 Forecast。</div>';
  const timezone = options.timezone || "Asia/Shanghai";
  const hasVitality = points.some((point) => Number.isFinite(Number(point.vitality_0_10)));
  const posterior = normalizedCurve(data?.retrospective_curve);
  const source = normalizedCurve(data?.retrospective_source_curve);
  const sourceMismatch = data?.retrospective_matches_current_forecast === false;
  const changes = significantChanges(points);
  const peakMinute = clockMinute(data?.analysis?.peak_stress_time);
  const peakValue = Number(data?.analysis?.peak_stress);
  const first = points[0];
  const last = points[points.length - 1];

  return `<div class="forecast-chart-shell">
    <div class="forecast-chart-scroll">
      <svg class="forecast-chart-svg" viewBox="0 0 ${WIDTH} 650" role="img" aria-label="${escapeHtml(data.local_date)} 压力与活力 Forecast">
        <rect class="risk-zone risk-zone-low" x="${LEFT}" y="186" width="${PLOT_WIDTH}" height="144"/>
        <rect class="risk-zone risk-zone-medium" x="${LEFT}" y="138" width="${PLOT_WIDTH}" height="48"/>
        <rect class="risk-zone risk-zone-high" x="${LEFT}" y="90" width="${PLOT_WIDTH}" height="48"/>
        ${calendarBands(data)}
        ${grid(90, 240, "心理压力（0–10）")}
        ${grid(410, 180, hasVitality ? "活力（0–10）" : "事件影响（0–1）")}
        ${warningMarkers(data)}
        <path class="forecast-line stress-line" d="${linePath(points, "stress_0_10", 90, 240)}"/>
        ${sourceMismatch && source.length ? `<path class="forecast-line retrospective-source-line" d="${linePath(source, "stress_0_10", 90, 240)}"/>` : ""}
        ${posterior.length ? `<path class="forecast-line posterior-line" d="${linePath(posterior, "stress_0_10", 90, 240)}"/>` : ""}
        ${hasVitality
          ? `<path class="forecast-line vitality-line" d="${linePath(points, "vitality_0_10", 410, 180)}"/>`
          : `<path class="forecast-line input-line" d="${linePath(points, "event_stress_input", 410, 180, 1)}"/>`}
        ${observationMarkers(data, timezone, 90, 240, "stress_0_10", "observation-marker stress-observation")}
        ${hasVitality ? observationMarkers(data, timezone, 410, 180, "energy_0_10", "observation-marker vitality-observation") : ""}
        ${reviewMarkers(data, 90, 240, "stress_0_10")}
        ${hasVitality ? reviewMarkers(data, 410, 180, "vitality_0_10") : ""}
        ${peakMinute !== null && Number.isFinite(peakValue) ? `<g class="peak-marker"><circle cx="${xForMinute(peakMinute)}" cy="${yForValue(peakValue, 90, 240)}" r="7"/><text x="${xForMinute(peakMinute)}" y="${yForValue(peakValue, 90, 240) - 14}" text-anchor="middle">峰值 ${escapeHtml(data.analysis.peak_stress_time)} · ${peakValue.toFixed(1)}</text></g>` : ""}
        ${changes.map((item) => `<circle class="change-marker" cx="${xForMinute(item.minute)}" cy="${yForValue(item.value, 90, 240)}" r="3"><title>${escapeHtml(item.time)} · ${item.delta >= 0 ? "+" : ""}${item.delta.toFixed(2)}</title></circle>`).join("")}
        <line class="forecast-axis" x1="${LEFT}" y1="590" x2="${RIGHT}" y2="590"/>
        ${timeAxis()}
      </svg>
    </div>
    <div class="forecast-legend" aria-label="图例">
      <span><i class="legend-line stress"></i>当前压力 Forecast</span>
      ${hasVitality ? '<span><i class="legend-line vitality"></i>活力 Forecast</span>' : '<span><i class="legend-line input"></i>事件影响</span>'}
      ${sourceMismatch && source.length ? '<span><i class="legend-line source"></i>回顾基准 Forecast</span>' : ""}
      ${posterior.length ? '<span><i class="legend-line posterior"></i>Daily Review 回顾估计</span>' : ""}
      <span><i class="legend-dot observation"></i>即时 Observation</span>
      <span><i class="legend-dot review"></i>Daily Review 反馈</span>
      <span><i class="legend-band calendar"></i>Calendar Event</span>
      <span><i class="legend-line warning"></i>Warning / Risk Window</span>
    </div>
    <div class="curve-readout">
      <span><b>${points.length}</b> 个权威曲线点</span>
      <span><b>${escapeHtml(first.time)}</b> 起点</span>
      <span><b>${escapeHtml(last.time)}</b> 当日最后一个采样点（非 terminal）</span>
      ${changes.map((item) => `<span><b>${escapeHtml(item.time)}</b> 明显${item.delta >= 0 ? "上升" : "回落"} ${Math.abs(item.delta).toFixed(2)}</span>`).join("")}
    </div>
  </div>`;
}
