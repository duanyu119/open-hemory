"use strict";

// All content comes from same-origin APIs. No samples, external assets or model calls.
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const list = (value) => Array.isArray(value) ? value : [];
const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
const TIME_ZONE = "Asia/Shanghai";
const state = { view: "conversations", date: "", dateTouched: false, dateInitialized: false, q: "", topic: "", overview: null, items: [], cursor: null, pages: 0, lastSuccess: null, listSuccess: null, snapshotKey: "", detail: null, detailKind: null, detailOrigin: null, csrf: null, editing: false, reprocessPreview: null, lanes: new Map() };
const statusLabels = { pending: "本地转写中", queued: "等待处理", running: "处理中", processing: "本地转写中", transcribed: "待整理", ready: "已整理", needs_review: "待核对", failed: "处理失败", budget_blocked: "额度待确认", succeeded: "已完成", done: "历史转写完成", no_speech: "未检测到语音", skipped: "已跳过", disabled: "未启用" };
const viewLabels = { conversations: ["对话", "从完整谈话出发，回看值得留下的内容。", "当日对话"], timeline: ["时间线", "沿着一天的顺序，回看谈话与其间的间隔。", "当日时间线"], recordings: ["原始录音", "查看完整原音，以及每个来源的转写进度。", "当日原音"], processing: ["处理状态", "查看本地分析、待核对项目与摘要服务状态。", "处理概览"] };

function dateOf(value = new Date()) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
  return ["year", "month", "day"].map((type) => parts.find((part) => part.type === type).value).join("-");
}
function timeOf(value, seconds = false) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { timeZone: TIME_ZONE, hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false }).format(date) : "—";
}
function clock(seconds) {
  const total = Math.max(0, Math.round(number(seconds)));
  const hours = Math.floor(total / 3600);
  return `${hours ? hours + ":" : ""}${hours ? String(Math.floor(total / 60) % 60).padStart(2, "0") : Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
function duration(seconds) {
  const total = Math.max(0, Math.round(number(seconds)));
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60);
  return hours ? `${hours} 小时${minutes ? ` ${minutes} 分` : ""}` : `${minutes} 分钟`;
}
function rangeLabel(item) {
  const startDay = dateOf(item.started_at), endDay = dateOf(item.ended_at);
  return `${timeOf(item.started_at)}—${endDay && startDay !== endDay ? endDay.slice(5) + " " : ""}${timeOf(item.ended_at)}`;
}
function badge(status, label) {
  const known = Object.prototype.hasOwnProperty.call(statusLabels, status);
  return `<span class="badge ${known ? esc(status) : ""}">${esc(label || statusLabels[status] || "状态待确认")}</span>`;
}
function titleOf(item) { return item.title || `${timeOf(item.started_at)} 对话 · ${statusLabels[item.status] || "正在整理"}`; }
function tagsOf(item) { return list(item.tags).map((tag) => typeof tag === "string" ? tag : tag.name || tag.title || "").filter(Boolean); }
function boundaryNote(value) {
  if (!value || ["confirmed", "complete", "manual", "final"].includes(value)) return "";
  return ["gap", "missing", "incomplete"].includes(value) ? "来源有缺口，连续性待核对" : "暂定边界 · 资料可能未齐";
}
function empty(title, description) { return `<div class="empty-state"><span class="empty-symbol" aria-hidden="true">◌</span><h3>${esc(title)}</h3><p>${esc(description)}</p></div>`; }
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $("toast").hidden = true; }, 5500);
}
function startLane(name) {
  state.lanes.get(name)?.abort();
  const controller = new AbortController();
  state.lanes.set(name, controller);
  return controller;
}
async function requestJSON(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...options.headers } });
  let data;
  try { data = await response.json(); } catch { throw new Error(`服务返回了无法读取的数据（HTTP ${response.status}）`); }
  if (!response.ok) {
    const message = typeof data.message === "string" ? data.message : typeof data.error === "string" ? data.error : "";
    const error = new Error(response.status === 409 ? "这条对话已有新版本。请重新载入详情，核对后再次操作；本次请求未提交。" : `请求失败（HTTP ${response.status}）${message ? "：" + message.slice(0, 240) : ""}`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function currentKey() { return [state.view, state.date, state.q, state.topic].join("|"); }
function listURL(cursor = null) {
  const params = new URLSearchParams({ date: state.date, limit: "40" });
  if (cursor) params.set("cursor", cursor);
  if (state.view !== "recordings") {
    if (state.q) params.set("q", state.q);
    if (state.topic) params.set("topic", state.topic);
  }
  return `/api/${state.view === "recordings" ? "chunks" : "conversations"}?${params}`;
}
function renderOverview(data) {
  $("stat-conversations").textContent = data.counts?.conversations ?? "—";
  $("stat-audio").textContent = data.counts?.audio_seconds == null ? "—" : duration(data.counts.audio_seconds);
  $("stat-speech").textContent = data.counts?.speech_seconds == null ? "—" : duration(data.counts.speech_seconds);
  $("stat-chunks").textContent = `${data.counts?.chunks ?? "—"} 段原音`;
  const attention = data.processing?.attention;
  $("stat-attention").textContent = Array.isArray(attention) ? attention.length : attention == null ? "—" : number(attention);
  const days = list(data.days).map((day) => typeof day === "string" ? day : day.date || day.day).filter((day) => /^\d{4}-\d{2}-\d{2}$/.test(day)).sort().reverse().slice(0, 12);
  const html = days.map((day) => `<button class="date-chip${day === state.date ? " active" : ""}" data-day="${esc(day)}"${day === state.date ? ' aria-current="date"' : ""}>${day === data.today ? "今天" : day.slice(5).replace("-", " / ")}</button>`).join("");
  if ($("date-strip").innerHTML !== html) $("date-strip").innerHTML = html;
}
function renderTopicFilter() {
  // Overview may provide the complete taxonomy; otherwise label the loaded-page scope.
  const provided = list(state.overview?.tags || state.overview?.topics).map((tag) => typeof tag === "string" ? tag : tag.name || tag.title || "");
  const tags = [...new Set([...provided, ...state.items.flatMap(tagsOf), state.topic].filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  $("topic").innerHTML = `<option value="">全部主题</option>${tags.map((tag) => `<option value="${esc(tag)}"${tag === state.topic ? " selected" : ""}>${esc(tag)}</option>`).join("")}`;
  $("topic").title = provided.length ? "筛选主题标签" : "标签来自当前已加载对话；选择后在当日全部对话中筛选";
}
function conversationCard(item) {
  const tags = tagsOf(item), boundary = boundaryNote(item.boundary_status);
  return `<article class="conversation-card" data-card-id="${esc(item.id)}">
    <div class="card-meta"><span class="time-range">${esc(rangeLabel(item))}</span>${badge(item.status)}</div>
    <h3 class="card-title"><button data-action="open-conversation" data-id="${esc(item.id)}">${esc(titleOf(item))}</button></h3>
    <p class="card-overview">${esc(item.overview || "摘要尚未就绪，可以先查看转写与原音。")}</p>
    <p class="card-duration">跨度 ${esc(duration(item.duration_seconds))} · 转写覆盖 ${item.speech_seconds == null ? "待分析" : esc(duration(item.speech_seconds))}${boundary ? `<br>${esc(boundary)}` : ""}</p>
    <div class="card-tags">${tags.slice(0, 3).map((tag) => `<button class="tag tag-button" data-action="filter-topic" data-topic="${esc(tag)}">${esc(tag)}</button>`).join("")}${tags.length > 3 ? `<span class="tag">+${tags.length - 3}</span>` : ""}</div>
    <div class="card-foot"><button class="play-link" data-action="play-conversation" data-id="${esc(item.id)}"><span class="play-circle" aria-hidden="true">▶</span>播放精简版</button><button data-action="open-conversation" data-id="${esc(item.id)}">${number(item.topic_count) > 0 ? `查看 ${number(item.topic_count)} 个主题` : "查看详情"} <span aria-hidden="true">↗</span></button></div>
  </article>`;
}
function renderTimeline(items) {
  const chronological = [...items].sort((a, b) => new Date(a.started_at) - new Date(b.started_at) || String(a.id).localeCompare(String(b.id)));
  let previous = null;
  return `<div class="timeline">${chronological.map((item) => {
    const gap = previous ? (new Date(item.started_at) - new Date(previous.ended_at)) / 1000 : 0;
    const gapHTML = gap >= 60 ? `<div class="timeline-gap">相隔 ${esc(duration(gap))} · 期间无对话记录</div>` : "";
    previous = item;
    return `${gapHTML}<div class="timeline-row"><time class="timeline-clock" datetime="${esc(item.started_at)}">${esc(timeOf(item.started_at))}</time>${conversationCard(item)}</div>`;
  }).join("")}</div>`;
}
function textState(chunk) {
  return ({ pending: "尚未完成本地转写", empty: "转写结果为空，不能据此判断没有语音", no_speech: "本地分析未检测到语音", failed: "转写失败，原音仍可回听", corrupt: "转写数据无法读取", missing: "尚无可读转写", unavailable: "转写暂不可用" })[chunk.text_state] || "尚无可读转写，可先回听完整原音。";
}
function recordingCard(chunk) {
  return `<article class="recording-card"><div class="recording-icon" aria-hidden="true">≋</div><div class="recording-content"><div class="card-meta"><span class="time-range">${esc(timeOf(chunk.started_at, true))}</span><span>${esc(duration(chunk.duration))}</span>${badge(chunk.status, chunk.status_label)}</div><h3>原始录音 · ${esc(timeOf(chunk.started_at))}</h3><p>${esc(chunk.text || textState(chunk))}</p></div><div class="recording-actions"><button class="text-button" data-action="play-chunk" data-id="${esc(chunk.chunk_id)}">播放</button><button class="text-button" data-action="open-chunk" data-id="${esc(chunk.chunk_id)}">详情 ↗</button></div></article>`;
}
function renderProcessing() {
  const processing = state.overview?.processing || {}, cloud = state.overview?.cloud || {};
  const stages = Object.entries(processing.states || {});
  const attention = list(processing.attention);
  const cost = (value) => value == null ? "尚未确认" : `$${number(value).toFixed(2)}`;
  return `<div class="status-grid"><section class="status-card"><h3>本地处理</h3>${stages.length ? stages.map(([key, value]) => `<div class="status-row"><span>${esc(statusLabels[key] || key)}</span><strong>${esc(typeof value === "object" ? value.count ?? "—" : value)}</strong></div>`).join("") : '<p>暂无处理队列信息。</p>'}<p>先完成本地语音分析与转写，再生成对话和主题。</p></section><section class="status-card"><h3>摘要服务</h3>${badge(cloud.enabled ? "ready" : "disabled", cloud.enabled ? "已启用" : "未启用")}<p>${esc(cloud.message || "摘要服务状态尚未提供。")}</p><div class="status-row"><span>已记录费用</span><strong>${esc(cost(cloud.spent_usd))}</strong></div><div class="status-row"><span>预算上限</span><strong>${esc(cost(cloud.budget_usd))}</strong></div><p>页面刷新只读取结果，不触发模型调用。</p></section><section class="status-card status-wide"><h3>需要留意</h3>${attention.length ? attention.map((entry) => `<div class="attention-item">${typeof entry === "string" ? esc(entry) : `<strong>${esc(entry.title || entry.status_label || statusLabels[entry.status] || "待核对项目")}</strong><p>${esc(entry.message || entry.reason || entry.last_error || "请查看来源详情。")}</p>${entry.conversation_id ? `<button class="text-button" data-action="open-conversation" data-id="${esc(entry.conversation_id)}">查看对话 ↗</button>` : entry.chunk_id ? `<button class="text-button" data-action="open-chunk" data-id="${esc(entry.chunk_id)}">查看原音 ↗</button>` : ""}`}</div>`).join("") : '<p>当前没有待核对项目。</p>'}</section></div>`;
}
function renderList() {
  $("list-count").textContent = state.view === "processing" ? "" : `已显示 ${state.items.length} ${state.view === "recordings" ? "段" : "条"}${state.cursor ? " · 还有更多" : ""}`;
  $("load-more").hidden = !state.cursor || state.view === "processing";
  if (state.view === "processing") { $("content").innerHTML = renderProcessing(); return; }
  if (!state.items.length) {
    $("content").innerHTML = state.q || state.topic ? empty("没有匹配的对话", "尝试更换关键词、主题或日期。") : state.view === "recordings" ? empty("这一天还没有原音", "手表停止录音并连上 Wi-Fi 后，已上传的录音会显示在这里。") : empty("这一天还没有整理出的对话", "可以切换日期，或在原始录音和处理状态中查看接收与转写进度。");
    return;
  }
  $("content").innerHTML = state.view === "recordings" ? `<div class="recording-list">${state.items.map(recordingCard).join("")}</div>` : state.view === "timeline" ? renderTimeline(state.items) : `<div class="card-grid">${state.items.map(conversationCard).join("")}</div>`;
  if (state.view !== "recordings") renderTopicFilter();
}
async function refresh({ manual = false, append = false, changed = false } = {}) {
  if (state.lanes.has("list") && !manual && !changed) return;
  const controller = startLane("list"), key = currentKey();
  const timeout = setTimeout(() => controller.abort("timeout"), 18000);
  const overviewOnly = !manual && !changed && state.pages > 1;
  const needsList = state.view !== "processing" && !overviewOnly;
  $("content").setAttribute("aria-busy", "true");
  $("load-more").disabled = true;
  if (changed) $("load-more").hidden = true;
  try {
    const [overview, result] = await Promise.all([
      requestJSON("/api/overview", { signal: controller.signal }),
      needsList ? requestJSON(listURL(append ? state.cursor : null), { signal: controller.signal }) : Promise.resolve(null)
    ]);
    if (controller.signal.aborted || key !== currentKey()) return;
    if (!overview.counts || (needsList && !Array.isArray(state.view === "recordings" ? result?.chunks : result?.items))) throw new Error("服务数据格式不完整，保留上次成功读取的内容。");
    state.overview = overview;
    if (!state.dateInitialized) {
      state.dateInitialized = true;
      if (!state.dateTouched && /^\d{4}-\d{2}-\d{2}$/.test(overview.today || "") && state.date !== overview.today) {
        state.date = overview.today;
        $("date").value = state.date;
        refresh({ changed: true });
        return;
      }
    }
    renderOverview(overview);
    if (result) {
      const incoming = state.view === "recordings" ? result.chunks : result.items;
      const different = JSON.stringify(incoming) !== JSON.stringify(state.items);
      const deferUpdate = !manual && !changed && !append && (!audio.paused || $("content").contains(document.activeElement)) && different && state.snapshotKey === key;
      if (deferUpdate) {
        $("list-hint").textContent = `列表截至 ${timeOf(state.listSuccess, true)} · 有新内容，点击刷新查看`;
      } else {
        const existing = append ? state.items : [];
        const unique = new Map(existing.map((item) => [item.id || item.chunk_id, item]));
        incoming.forEach((item) => unique.set(item.id || item.chunk_id, item));
        state.items = [...unique.values()];
        state.cursor = result.next_cursor || null;
        state.pages = append ? state.pages + 1 : 1;
        state.snapshotKey = key;
        state.listSuccess = overview.server_time || new Date();
        if (different || append || changed || manual || !state.lastSuccess) renderList();
        $("list-hint").textContent = state.view === "timeline" ? "按时间正序 · 间隔不代表静音" : "按开始时间倒序";
        if (state.detailKind === "conversation" && state.detail) {
          const updated = incoming.find((item) => item.id === state.detail.id);
          if (updated && updated.revision !== state.detail.revision) showNewRevision();
        }
      }
    } else if (state.view === "processing") renderList();
    if (overviewOnly) $("list-hint").textContent = `列表截至 ${timeOf(state.listSuccess, true)} · 刷新可读取新内容`;
    state.lastSuccess = new Date();
    $("updated-at").textContent = `数据截至 ${timeOf(overview.server_time || state.lastSuccess, true)}`;
    $("connection").textContent = "已连接本机";
    $("connection").classList.remove("error");
    $("load-error").hidden = true;
  } catch (error) {
    if (controller.signal.aborted && controller.signal.reason !== "timeout") return;
    if (!controller.signal.aborted) controller.abort("failed");
    $("connection").textContent = "更新失败";
    $("connection").classList.add("error");
    $("load-error").hidden = false;
    const oldFilter = state.snapshotKey && state.snapshotKey !== key ? "当前仍显示上一次筛选的结果。" : "";
    $("load-error").textContent = `${controller.signal.reason === "timeout" ? "连接超时，请稍后重试。" : error.message} ${state.lastSuccess ? `保留上次成功数据（${timeOf(state.lastSuccess, true)}）。${oldFilter}` : "尚未成功读取数据，请确认本机看板服务可用。"}`;
    if (!state.lastSuccess) $("content").innerHTML = empty("暂时无法读取资料", "恢复连接后点击右上角刷新；已有录音不会受影响。");
  } finally {
    clearTimeout(timeout);
    if (state.lanes.get("list") === controller) {
      state.lanes.delete("list");
      $("content").setAttribute("aria-busy", "false");
      $("load-more").disabled = false;
    }
  }
}
function setView(view) {
  if (!viewLabels[view]) return;
  state.view = view;
  const labels = viewLabels[view];
  $("page-title").textContent = labels[0];
  $("page-description").textContent = labels[1];
  $("list-title").textContent = labels[2];
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  $("filters").hidden = view === "processing";
  $("date-strip").hidden = view === "processing";
  $("search").parentElement.hidden = view === "recordings";
  $("topic-filter").hidden = view === "recordings";
  $("load-more").hidden = true;
  refresh({ changed: true });
}
function setDate(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
  state.dateTouched = true;
  state.date = day;
  $("date").value = day;
  refresh({ changed: true });
}

// A single audio element lives outside the refreshed list and dialog body.
const audio = $("audio");
const playback = { conversation: null, mode: "compact", spans: [], index: 0, chunk: null, title: "", token: 0, pending: null, error: "", scope: "", fallback: false };
function validSpans(spans, chunks) {
  const order = new Map(chunks.map((chunk, index) => [chunk.chunk_id, index]));
  const durations = new Map(chunks.map((chunk) => [chunk.chunk_id, number(chunk.duration) * 1000]));
  const sorted = spans.filter((span) => order.has(span.chunk_id) && Number.isFinite(Number(span.start_ms)) && Number.isFinite(Number(span.end_ms))).map((span) => ({ chunk_id: span.chunk_id, start_ms: Math.max(0, number(span.start_ms)), end_ms: durations.get(span.chunk_id) > 0 ? Math.min(number(span.end_ms), durations.get(span.chunk_id)) : number(span.end_ms) })).filter((span) => span.end_ms > span.start_ms).sort((a, b) => order.get(a.chunk_id) - order.get(b.chunk_id) || a.start_ms - b.start_ms);
  const merged = [];
  sorted.forEach((span) => {
    const last = merged[merged.length - 1];
    if (last && last.chunk_id === span.chunk_id && span.start_ms <= last.end_ms) last.end_ms = Math.max(last.end_ms, span.end_ms);
    else merged.push({ ...span });
  });
  return merged;
}
function sortedChunks(conversation) {
  return [...list(conversation.chunks)].sort((a, b) => new Date(a.started_at) - new Date(b.started_at) || String(a.chunk_id).localeCompare(String(b.chunk_id)));
}
function subtractHidden(spans, hidden) {
  let result = spans;
  hidden.forEach((cut) => {
    result = result.flatMap((span) => {
      if (cut.chunk_id !== span.chunk_id || cut.end_ms <= span.start_ms || cut.start_ms >= span.end_ms) return [span];
      const parts = [];
      if (cut.start_ms > span.start_ms) parts.push({ ...span, end_ms: cut.start_ms });
      if (cut.end_ms < span.end_ms) parts.push({ ...span, start_ms: cut.end_ms });
      return parts;
    });
  });
  return result;
}
function playbackSpans(conversation, mode) {
  const chunks = sortedChunks(conversation);
  const full = validSpans(chunks.map((chunk) => ({ chunk_id: chunk.chunk_id, start_ms: 0, end_ms: number(chunk.duration) * 1000 })), chunks);
  playback.fallback = false;
  if (mode === "full") return full;
  const regions = list(conversation.regions);
  const retained = validSpans(regions.filter((region) => ["speech", "uncertain", "mixed"].includes(region.kind)), chunks);
  const excluded = validSpans(regions.filter((region) => ["silence", "non_speech"].includes(region.kind)), chunks);
  // ASR can miss audible speech. Only explicit acoustic exclusions shorten clean playback;
  // speech/uncertain/mixed take precedence if region labels overlap. Unclassified gaps stay.
  const confirmedExclusions = subtractHidden(excluded, retained);
  const classified = validSpans([...retained, ...excluded], chunks);
  playback.fallback = subtractHidden(full, classified).length > 0;
  const hidden = validSpans(list(conversation.utterances).filter((utterance) => utterance.hidden).flatMap((utterance) => list(utterance.source_spans)), chunks);
  return subtractHidden(subtractHidden(full, confirmedExclusions), hidden);
}
function playerError(message) { playback.error = message; syncPlayer(); }
function syncPlayer() {
  const span = playback.spans[playback.index];
  const chunks = playback.conversation ? sortedChunks(playback.conversation) : [];
  const chunkIndex = chunks.findIndex((chunk) => chunk.chunk_id === playback.chunk);
  const length = Number.isFinite(audio.duration) ? audio.duration : number(chunks[chunkIndex]?.duration);
  const current = audio.currentTime || 0;
  const note = span ? `${playback.scope ? playback.scope + " · " : ""}原音 ${chunkIndex + 1}/${chunks.length} · 区间 ${playback.index + 1}/${playback.spans.length}（${clock(span.start_ms / 1000)}–${clock(span.end_ms / 1000)}）${playback.fallback ? " · 未分类区间保守保留" : ""}${chunks.length > 1 ? " · 跨文件有短暂间隙" : ""}` : "";
  document.querySelectorAll("[data-player-title]").forEach((element) => { element.textContent = playback.title; });
  document.querySelectorAll("[data-player-note]").forEach((element) => { element.textContent = note; });
  document.querySelectorAll("[data-player-time]").forEach((element) => { element.textContent = `${clock(current)} / ${clock(length)}`; });
  document.querySelectorAll('[data-player="toggle"]').forEach((button) => { button.textContent = audio.paused ? "▶" : "Ⅱ"; button.setAttribute("aria-label", audio.paused ? "播放" : "暂停"); });
  document.querySelectorAll('[data-player="previous"]').forEach((button) => { button.disabled = playback.index <= 0; });
  document.querySelectorAll('[data-player="next"]').forEach((button) => { button.disabled = playback.index >= playback.spans.length - 1; });
  document.querySelectorAll("[data-player-seek]").forEach((input) => { if (document.activeElement !== input) { input.max = length || 1; input.value = current; } input.setAttribute("aria-valuetext", `原音 ${clock(current)}，总长 ${clock(length)}`); });
  document.querySelectorAll("[data-player-mode]").forEach((input) => { input.value = playback.mode; });
  document.querySelectorAll("[data-player-error]").forEach((element) => { element.hidden = !playback.error; element.textContent = playback.error; });
  $("detail-player").hidden = !playback.conversation;
}
async function playAudio(token) {
  try { await audio.play(); } catch (error) { if (token === playback.token && error.name !== "AbortError") playerError("播放未开始。请点击播放重试，并检查本机音频服务。"); }
}
function activateSpan(index, autoplay = true, seekSeconds = null) {
  if (!playback.spans[index]) return;
  playback.index = index;
  playback.error = "";
  const span = playback.spans[index], token = ++playback.token;
  const target = seekSeconds == null ? span.start_ms / 1000 : Math.max(span.start_ms / 1000, Math.min(seekSeconds, (span.end_ms - 1) / 1000));
  const changedFile = playback.chunk !== span.chunk_id;
  playback.chunk = span.chunk_id;
  playback.pending = { token, target, autoplay };
  if (changedFile) {
    audio.pause();
    audio.src = `/api/audio/${encodeURIComponent(span.chunk_id)}`;
    audio.load();
  } else if (audio.readyState >= 1) applyPendingSeek();
  syncPlayer();
}
function applyPendingSeek() {
  const pending = playback.pending;
  if (!pending || pending.token !== playback.token) return;
  playback.pending = null;
  try { audio.currentTime = pending.target; } catch { playerError("暂时无法定位原音，请重新播放。"); return; }
  if (pending.autoplay) playAudio(pending.token);
}
function resumeConversationMode(conversation, mode) {
  if (!playback.conversation || playback.conversation.id !== conversation.id || playback.conversation.revision !== conversation.revision) return false;
  if (!changePlaybackMode(mode)) return true;
  if (playback.pending) playback.pending.autoplay = true;
  else playAudio(playback.token);
  return true;
}
function startPlayback(conversation, spans = null, scope = "") {
  if (!spans && resumeConversationMode(conversation, "compact")) return true;
  const playlist = spans ? validSpans(spans, sortedChunks(conversation)) : playbackSpans(conversation, "compact");
  if (!playlist.length) { toast("暂无可播放的语音区间，请在详情中选择完整原音。"); return false; }
  playback.conversation = conversation;
  playback.mode = "compact";
  playback.spans = playlist;
  playback.scope = scope;
  playback.title = titleOf(conversation);
  if (spans) playback.fallback = false;
  $("player").hidden = false;
  activateSpan(0);
  return true;
}
function startFullPlayback(conversation) {
  if (resumeConversationMode(conversation, "full")) return;
  const spans = playbackSpans(conversation, "full");
  if (!spans.length) { toast("原音时长尚未就绪，暂时无法播放。"); return; }
  playback.conversation = conversation;
  playback.mode = "full";
  playback.spans = spans;
  playback.scope = "";
  playback.title = titleOf(conversation);
  $("player").hidden = false;
  activateSpan(0);
}
function changePlaybackMode(mode) {
  if (!playback.conversation) return false;
  const oldChunk = playback.chunk, oldTime = playback.pending?.target ?? audio.currentTime, wasPlaying = playback.pending?.autoplay ?? !audio.paused;
  const spans = playbackSpans(playback.conversation, mode);
  if (!spans.length) { playerError("暂无可播放的精简区间，完整原音仍可回听。"); syncPlayer(); return false; }
  playback.mode = mode;
  playback.scope = "";
  playback.spans = spans;
  let index = spans.findIndex((span) => span.chunk_id === oldChunk && oldTime * 1000 >= span.start_ms && oldTime * 1000 < span.end_ms);
  const keepPosition = index >= 0;
  if (index < 0) index = spans.findIndex((span) => span.chunk_id === oldChunk && span.start_ms >= oldTime * 1000);
  if (index < 0) {
    const order = sortedChunks(playback.conversation).map((chunk) => chunk.chunk_id);
    index = spans.findIndex((span) => order.indexOf(span.chunk_id) > order.indexOf(oldChunk));
  }
  if (index < 0) index = spans.length - 1;
  activateSpan(index, wasPlaying, keepPosition ? oldTime : null);
  return true;
}
function advancePlayback() {
  if (playback.index + 1 < playback.spans.length) activateSpan(playback.index + 1);
  else { audio.pause(); syncPlayer(); }
}
audio.addEventListener("loadedmetadata", applyPendingSeek);
audio.addEventListener("timeupdate", () => {
  const span = playback.spans[playback.index];
  if (span && !audio.paused && !audio.seeking && !playback.pending && audio.currentTime * 1000 >= span.end_ms - 40) advancePlayback();
  syncPlayer();
});
audio.addEventListener("ended", () => {
  const span = playback.spans[playback.index];
  if (!playback.pending && span && audio.currentTime * 1000 >= span.end_ms - 40) advancePlayback();
});
audio.addEventListener("error", () => { playback.pending = null; playerError("原音加载失败，已保留当前位置。请点击播放重试或切换区间。"); });
["play", "pause", "durationchange", "seeked"].forEach((event) => audio.addEventListener(event, syncPlayer));
document.querySelectorAll("[data-player]").forEach((button) => button.addEventListener("click", () => {
  if (!playback.spans.length) return;
  const action = button.dataset.player;
  if (action === "previous") activateSpan(playback.index - 1);
  else if (action === "next") activateSpan(playback.index + 1);
  else if (!audio.paused) audio.pause();
  else {
    const span = playback.spans[playback.index];
    if (audio.error) { playback.chunk = null; activateSpan(playback.index, true, audio.currentTime); }
    else if (audio.currentTime * 1000 >= span.end_ms - 40) activateSpan(playback.index === playback.spans.length - 1 ? 0 : playback.index + 1);
    else { playback.error = ""; playAudio(playback.token); }
  }
}));
document.querySelectorAll("[data-player-mode]").forEach((input) => input.addEventListener("change", () => changePlaybackMode(input.value)));
document.querySelectorAll("[data-player-seek]").forEach((input) => input.addEventListener("input", () => {
  const target = number(input.value), spans = playback.spans;
  let index = spans.findIndex((span) => span.chunk_id === playback.chunk && span.start_ms <= target * 1000 && span.end_ms > target * 1000);
  if (index < 0) index = spans.findIndex((span) => span.chunk_id === playback.chunk && span.start_ms >= target * 1000);
  if (index < 0) index = spans.map((span, i) => span.chunk_id === playback.chunk ? i : -1).filter((i) => i >= 0).pop();
  if (index != null && index >= 0) activateSpan(index, !audio.paused, target);
}));

function pointText(point) { return typeof point === "string" ? point : point?.text || point?.title || point?.action || point?.question || ""; }
function evidenceIDs(point) { return typeof point === "object" && point ? list(point.evidence_ids || point.utterance_ids).map(String) : []; }
function evidenceButton(ids, label = "原话 ↗") {
  return ids.length ? `<button class="evidence-button" data-action="evidence" data-ids="${esc(JSON.stringify(ids))}">${esc(label)}</button>` : "";
}
function pointList(points) {
  return `<ul class="key-points">${list(points).map((point) => `<li>${esc(pointText(point))}${evidenceButton(evidenceIDs(point))}</li>`).join("")}</ul>`;
}
function sectionOf(title, points) { return list(points).length ? `<section class="detail-section"><h3>${esc(title)}</h3>${pointList(points)}</section>` : ""; }
function utteranceElementID(id) { return `utterance-${encodeURIComponent(id)}`; }
function utteranceLabel(utterance, conversation) {
  const span = list(utterance.source_spans)[0];
  const chunk = list(conversation.chunks).find((entry) => entry.chunk_id === span?.chunk_id);
  if (!span || !chunk?.started_at) return "暂无模型时间戳";
  const instant = new Date(new Date(chunk.started_at).getTime() + number(span.start_ms));
  return `${timeOf(instant, true)} · 模型时间戳 · 原音 ${clock(span.start_ms / 1000)}${list(utterance.source_spans).length > 1 ? " · 跨多个来源区间" : ""}`;
}
function renderUtterance(utterance, conversation) {
  return `<article class="utterance${utterance.hidden ? " is-hidden" : ""}" id="${esc(utteranceElementID(utterance.id))}" tabindex="-1"${utterance.hidden ? ' data-hidden-utterance hidden' : ""}><p>${esc(utterance.text || "（此发言暂无可读文字）")}</p>${list(utterance.quality_flags).length ? `<p class="notice warning">${esc(utterance.quality_flags.join("；"))}。暂不作为摘要证据。</p>` : ""}<div class="utterance-tools"><span class="source-time">${esc(utteranceLabel(utterance, conversation))}</span>${list(utterance.source_spans).length ? evidenceButton([String(utterance.id)], "回听原话") : ""}<button data-action="edit-utterance" data-id="${esc(utterance.id)}">改字</button><button data-action="hide-utterance" data-id="${esc(utterance.id)}">${utterance.hidden ? "恢复" : "隐藏"}</button></div></article>`;
}
function renderTopic(topic) {
  return `<article class="topic-card"><div class="topic-head"><h4>${esc(topic.title || "待命名主题")}</h4></div>${list(topic.key_points).length ? pointList(topic.key_points) : '<p class="muted">此主题尚无整理好的要点。</p>'}${list(topic.utterance_ids).length ? `<button class="topic-play" data-action="evidence" data-ids="${esc(JSON.stringify(topic.utterance_ids))}">▶ 回听主题原话 · ${topic.utterance_ids.length} 段发言</button>` : ""}<details class="topic-manage"><summary>整理此主题</summary><div class="manage-actions"><button class="secondary-button" data-action="rename-topic" data-id="${esc(topic.id)}">修改名称</button><button class="secondary-button" data-action="split-topic" data-id="${esc(topic.id)}">拆出新主题</button></div></details></article>`;
}
function sourceMarkup(conversation) {
  const kinds = { speech: "语音", silence: "静音", non_speech: "非语音", uncertain: "不确定，精简版保留", mixed: "混合声音，精简版保留" };
  return `<details class="source-section"><summary>来源与播放区间 · ${list(conversation.chunks).length} 段原音</summary><p>时间均为北京时间。模型时间戳定位到原文件时间轴，可能有偏差，尚未逐句人工核验。精简播放保留语音、不确定、混合及未分类区间；仅跳过明确静音、非语音及手动隐藏的发言范围。跨文件播放可能有短暂间隙。</p>${sortedChunks(conversation).map((chunk) => `<div class="source-row"><strong>${esc(dateOf(chunk.started_at))} ${esc(timeOf(chunk.started_at, true))}</strong> · ${esc(duration(chunk.duration))}<br><code>${esc(chunk.chunk_id)}</code>${chunk.sha256 ? `<br><span>SHA-256：${esc(chunk.sha256)}</span>` : ""}<br><button class="text-button" data-action="open-chunk" data-id="${esc(chunk.chunk_id)}">查看完整原音与转写 ↗</button>${list(conversation.regions).filter((region) => region.chunk_id === chunk.chunk_id).map((region) => `<div class="source-region">${clock(region.start_ms / 1000)}–${clock(region.end_ms / 1000)} · ${esc(kinds[region.kind] || "区间待核对")}${region.reason ? " · " + esc(region.reason) : ""}</div>`).join("")}</div>`).join("")}</details>`;
}
function renderConversationDetail(conversation) {
  $("detail-title").textContent = titleOf(conversation);
  $("detail-kicker").textContent = `${dateOf(conversation.started_at)} · 对话详情`;
  const boundary = boundaryNote(conversation.boundary_status), utterances = list(conversation.utterances);
  $("detail-body").innerHTML = `<div class="detail-meta"><span>${esc(rangeLabel(conversation))}</span><span>跨度 ${esc(duration(conversation.duration_seconds))}</span><span>转写覆盖 ${conversation.speech_seconds == null ? "待分析" : esc(duration(conversation.speech_seconds))}</span>${badge(conversation.status)}${conversation.manual ? '<span class="badge ready">已人工整理</span>' : ""}</div><div id="new-version" class="notice new-version" hidden>这条对话已有新版本；当前原音与阅读位置保持不变。<button class="text-button" data-action="reload-detail">载入新版本</button></div>${boundary ? `<p class="notice warning">${esc(boundary)}。对话范围可能随新资料补齐而更新。</p>` : ""}${list(conversation.summary_review_reasons).map((reason) => `<p class="notice warning">${esc(reason)}</p>`).join("")}<div class="detail-actions"><button class="primary-button" data-action="play-detail">▶ 播放精简版</button><button class="secondary-button" data-action="play-full">完整原音</button><button class="secondary-button" data-action="rename">修改标题</button></div><section class="detail-section"><h3>概述</h3><p class="detail-overview">${esc(conversation.overview || "摘要尚未就绪。你可以先阅读已完成的转写，或回听原音。")}</p></section>${list(conversation.topics).length ? `<section class="detail-section"><h3>主题 · ${conversation.topics.length}</h3>${conversation.topics.map(renderTopic).join("")}</section>` : '<section class="detail-section"><h3>主题</h3><p class="muted">主题整理尚未就绪。</p></section>'}${sectionOf("关键要点", conversation.key_points)}${sectionOf("已提到的决定", conversation.decisions)}${sectionOf("行动候选 · 待确认", conversation.action_candidates)}${sectionOf("待确认的问题", conversation.open_questions)}<section class="detail-section"><h3>原文与证据</h3><p class="edit-hint">转写覆盖是模型转写句段区间的并集，不代表 VAD 检测的实际人声时长。原话回听使用模型时间戳，可能有偏差；人工改字不等于已核对时间定位。</p><details class="transcript-section" id="transcript"><summary>展开转写 · ${utterances.length} 段发言</summary>${utterances.some((entry) => entry.hidden) ? '<label class="checkbox-label"><input id="show-hidden" type="checkbox"> 显示已隐藏发言</label>' : ""}${utterances.length ? utterances.map((entry) => renderUtterance(entry, conversation)).join("") : '<p class="muted">尚无带时间定位的转写。来源详情中可能保留历史转写。</p>'}</details></section>${sourceMarkup(conversation)}<details class="manage-panel" id="manage"><summary>整理与修订</summary><p>修改会保存为新版本，原始录音保留。合并、拆分和隐藏操作可撤销；重算不会随刷新自动触发。</p><div class="manage-actions"><button class="secondary-button" data-action="rename">修改标题</button><button class="secondary-button" data-action="merge">合并对话</button><button class="secondary-button" data-action="split">拆分对话</button><button class="secondary-button" data-action="merge-topics">合并主题</button><button class="secondary-button" data-action="undo">撤销上次整理</button><button class="secondary-button" data-action="preview-reprocess">查看重算范围</button></div></details><div id="edit-area" aria-live="polite"></div>`;
}
function renderChunkDetail(chunk, backID = null) {
  $("detail-title").textContent = `原始录音 · ${timeOf(chunk.started_at)}`;
  $("detail-kicker").textContent = `${dateOf(chunk.started_at)} · 来源详情`;
  $("detail-body").innerHTML = `<div class="detail-meta"><span>${esc(timeOf(chunk.started_at, true))}</span><span>${esc(duration(chunk.duration))}</span>${badge(chunk.status, chunk.status_label)}</div><div class="detail-actions"><button class="primary-button" data-action="play-raw-detail">▶ 播放完整原音</button>${backID ? `<button class="secondary-button" data-action="open-conversation" data-id="${esc(backID)}">返回对话</button>` : ""}</div><section class="detail-section"><h3>转写</h3><p class="raw-transcript">${esc(chunk.text || textState(chunk))}</p></section><details class="source-section"><summary>来源信息</summary><table class="source-table"><tbody><tr><th scope="row">原音 ID</th><td>${esc(chunk.chunk_id)}</td></tr><tr><th scope="row">开始时间</th><td>${esc(dateOf(chunk.started_at))} ${esc(timeOf(chunk.started_at, true))}（北京时间）</td></tr><tr><th scope="row">时长</th><td>${esc(duration(chunk.duration))}</td></tr>${chunk.sha256 ? `<tr><th scope="row">SHA-256</th><td>${esc(chunk.sha256)}</td></tr>` : ""}${chunk.text_state ? `<tr><th scope="row">文字状态</th><td>${esc(chunk.text_state)}</td></tr>` : ""}</tbody></table><p>没有句级时间戳的历史转写仅能定位到整段原音。</p></details>`;
}
function showNewRevision() { const notice = $("new-version"); if (notice) notice.hidden = false; }
async function openDetail(id, kind = "conversation", autoplay = false) {
  if (state.editing) { toast("正在保存整理，请稍候。"); return; }
  const backID = kind === "chunk" && state.detailKind === "conversation" ? state.detail?.id : null;
  const controller = startLane("detail");
  const timeout = setTimeout(() => controller.abort("timeout"), 18000);
  if (!$("detail").open) { state.detailOrigin = document.activeElement; $("detail").showModal(); }
  state.reprocessPreview = null;
  state.lanes.get("preview")?.abort();
  state.detail = null;
  state.detailKind = kind;
  $("detail-title").textContent = "正在读取…";
  $("detail-body").innerHTML = '<p class="muted">正在读取详情与来源信息。</p>';
  $("detail-kicker").textContent = kind === "conversation" ? "对话详情" : "来源详情";
  $("detail-scroll").scrollTop = 0;
  $("detail-title").focus();
  syncPlayer();
  try {
    const result = await requestJSON(kind === "conversation" ? `/api/conversations/${encodeURIComponent(id)}` : `/api/chunk/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (controller.signal.aborted || !$("detail").open) return;
    const detail = kind === "chunk" ? result.chunk || result : result;
    if (!(kind === "conversation" ? detail.id : detail.chunk_id)) throw new Error("详情数据不完整，请重新载入。");
    state.detail = detail;
    if (kind === "conversation") renderConversationDetail(detail); else renderChunkDetail(detail, backID);
    if (autoplay) { if (kind === "conversation") startPlayback(detail); else playRawChunk(detail); }
  } catch (error) {
    if (controller.signal.aborted && controller.signal.reason !== "timeout") return;
    $("detail-title").textContent = "暂时无法读取详情";
    $("detail-body").innerHTML = `<p class="notice warning">${esc(controller.signal.reason === "timeout" ? "详情读取超时。" : error.message)}</p><button class="secondary-button" data-action="${kind === "chunk" ? "open-chunk" : "open-conversation"}" data-id="${esc(id)}">重试</button>`;
  } finally { clearTimeout(timeout); if (state.lanes.get("detail") === controller) state.lanes.delete("detail"); }
}
function closeDetail() {
  if (state.editing) { toast("正在保存整理，请等待本次请求结束。"); return; }
  $("detail").close();
}
$("detail-close").addEventListener("click", closeDetail);
$("detail").addEventListener("cancel", (event) => { event.preventDefault(); closeDetail(); });
$("detail").addEventListener("close", () => {
  state.lanes.get("detail")?.abort();
  state.lanes.get("candidates")?.abort();
  state.lanes.get("preview")?.abort();
  state.detail = null;
  state.reprocessPreview = null;
  state.detailKind = null;
  if (state.detailOrigin?.isConnected) state.detailOrigin.focus(); else $("main").focus();
});
$("detail").addEventListener("click", (event) => {
  if (event.target === $("detail")) {
    const rect = $("detail").getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDetail();
  }
});
function playRawChunk(chunk) {
  startFullPlayback({ id: chunk.chunk_id, title: `原始录音 · ${timeOf(chunk.started_at)}`, started_at: chunk.started_at, chunks: [chunk], regions: [], utterances: [] });
}
function revealEvidence(ids) {
  const conversation = state.detail;
  if (!conversation || state.detailKind !== "conversation") return;
  const selected = list(conversation.utterances).filter((entry) => ids.includes(String(entry.id)));
  if (!selected.length) { toast("原话证据暂不可用，请重新载入详情核对。"); return; }
  $("transcript").open = true;
  document.querySelectorAll(".utterance.highlight").forEach((element) => element.classList.remove("highlight"));
  selected.forEach((entry) => {
    const element = $(utteranceElementID(entry.id));
    if (element) { element.hidden = false; element.classList.add("highlight"); }
  });
  const first = $(utteranceElementID(selected[0].id));
  first?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  first?.focus({ preventScroll: true });
  const spans = selected.flatMap((entry) => list(entry.source_spans));
  if (spans.length) startPlayback(conversation, spans, "原话证据（模型时间戳）"); else toast("已定位原文；这段文字尚无可用的音频时间戳。");
}
function editShell(title, content, operation, submitLabel = "保存修改") {
  const area = $("edit-area");
  if (!area || !state.detail) return;
  area.innerHTML = `<form class="edit-form" id="correction-form" data-operation="${esc(operation)}" data-revision="${esc(state.detail.revision)}" data-conversation-id="${esc(state.detail.id)}"><h4>${esc(title)}</h4>${content}<p class="edit-hint">将基于当前版本保存修订，原始录音不变。</p><div class="form-actions"><button class="primary-button" type="submit">${esc(submitLabel)}</button><button class="secondary-button" type="button" data-action="cancel-edit">取消</button></div><div id="edit-error" class="edit-error" role="alert" hidden></div></form>`;
  area.scrollIntoView({ block: "center", behavior: "smooth" });
  const first = area.querySelector("input:not([type=hidden]),textarea,select,button");
  first?.focus({ preventScroll: true });
}
function selectedUtterance(id) { return list(state.detail?.utterances).find((entry) => String(entry.id) === String(id)); }
function selectedTopic(id) { return list(state.detail?.topics).find((entry) => String(entry.id) === String(id)); }
function utteranceOptions(utterances) { return utterances.map((entry) => `<option value="${esc(entry.id)}">${esc(utteranceLabel(entry, state.detail))} · ${esc(String(entry.text || "（暂无文字）").slice(0, 80))}</option>`).join(""); }
async function beginEdit(action, id) {
  if (!state.detail || state.detailKind !== "conversation" || state.editing) return;
  state.reprocessPreview = null;
  state.lanes.get("preview")?.abort();
  const conversation = state.detail;
  if (action === "rename") editShell("修改对话标题", `<label>标题<input name="title" type="text" value="${esc(conversation.title || "")}" maxlength="200" required></label>`, "rename");
  if (action === "edit-utterance") {
    const utterance = selectedUtterance(id);
    if (!utterance) return;
    editShell("修改转写文字", `<p class="edit-hint">${esc(utteranceLabel(utterance, conversation))}。本次改字保留原有来源定位。</p><input name="utterance_id" type="hidden" value="${esc(id)}"><label>转写文字<textarea name="text" maxlength="20000" required>${esc(utterance.text)}</textarea></label>`, "edit_utterance");
  }
  if (action === "hide-utterance") {
    const utterance = selectedUtterance(id);
    if (!utterance) return;
    editShell(utterance.hidden ? "恢复这段发言" : "隐藏这段发言", `<p class="detail-overview">${esc(utterance.text)}</p><p class="edit-hint">${utterance.hidden ? "恢复后重新显示在原文中，并纳入下一次精简播放。" : "隐藏后从默认原文与下一次精简播放中排除，已加载的播放队列不变。完整原音仍可回听。"}</p><input name="utterance_id" type="hidden" value="${esc(id)}"><input name="hidden" type="hidden" value="${!utterance.hidden}">`, "hide_utterance", utterance.hidden ? "确认恢复" : "确认隐藏");
  }
  if (action === "split") {
    const utterances = list(conversation.utterances);
    if (utterances.length < 2) { toast("至少需要两段带来源的发言才能拆分对话。"); return; }
    editShell("从选定发言开始拆为新对话", `<p class="edit-hint">选定发言及其后的内容将形成新对话，前面的内容保留。</p><label>新对话的第一段发言<select name="at_utterance_id" required>${utteranceOptions(utterances.slice(1))}</select></label>`, "split", "确认拆分");
  }
  if (action === "undo") editShell("撤销上次人工整理", '<p>撤销当前对话最近一次可撤销的人工修订。模型处理结果与原始录音不会被删除。</p>', "undo", "确认撤销");
  if (action === "rename-topic") {
    const topic = selectedTopic(id);
    if (!topic) return;
    editShell("修改主题名称", `<input name="topic_id" type="hidden" value="${esc(id)}"><label>主题名称<input name="title" type="text" maxlength="200" value="${esc(topic.title)}" required></label>`, "rename_topic");
  }
  if (action === "merge-topics") {
    const topics = list(conversation.topics);
    if (topics.length < 2) { toast("至少需要两个主题才能合并。"); return; }
    editShell("合并主题", `<fieldset><legend>选择至少两个主题</legend>${topics.map((topic) => `<label class="checkbox-label"><input type="checkbox" name="topic_ids" value="${esc(topic.id)}">${esc(topic.title)}</label>`).join("")}</fieldset>`, "merge_topics", "确认合并");
  }
  if (action === "split-topic") {
    const topic = selectedTopic(id);
    const utterances = list(conversation.utterances).filter((entry) => list(topic?.utterance_ids).includes(entry.id));
    if (utterances.length < 2) { toast("此主题至少需要两段发言才能拆分。"); return; }
    editShell("将部分发言拆为新主题", `<input name="topic_id" type="hidden" value="${esc(id)}"><label>新主题名称<input name="title" type="text" maxlength="200" required></label><fieldset data-total="${utterances.length}"><legend>选择归入新主题的发言，至少留一段在原主题</legend>${utterances.map((entry) => `<label class="checkbox-label"><input type="checkbox" name="utterance_ids" value="${esc(entry.id)}">${esc(String(entry.text || "（暂无文字）").slice(0, 240))}</label>`).join("")}</fieldset>`, "split_topic", "确认拆分");
  }
  if (action === "merge") {
    editShell("合并对话", '<p class="muted">正在读取同日可选对话…</p>', "merge", "确认合并");
    const form = $("correction-form");
    form.querySelector('[type="submit"]').disabled = true;
    const controller = startLane("candidates"), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const result = await requestJSON(`/api/conversations?${new URLSearchParams({ date: dateOf(conversation.started_at), limit: "100" })}`, { signal: controller.signal });
      if (state.detail !== conversation || !form.isConnected) return;
      const candidates = list(result.items).filter((entry) => entry.id !== conversation.id);
      if (!candidates.length) { form.innerHTML = '<h4>暂无可合并的同日对话</h4><p class="edit-hint">当前对话保持不变。</p><button class="secondary-button" type="button" data-action="cancel-edit">关闭</button>'; return; }
      editShell("合并对话", `<p class="edit-hint">将“${esc(titleOf(conversation))}”与选定对话合并。请确认两者属于同一次谈话；来源中的时间间隔会保留。</p><label>选择另一条对话<select name="other_id" required>${candidates.map((entry) => `<option value="${esc(entry.id)}" data-revision="${esc(entry.revision)}">${esc(rangeLabel(entry))} · ${esc(titleOf(entry))}</option>`).join("")}</select></label>${result.next_cursor ? '<p class="edit-hint">仅显示同日最近 100 条对话。</p>' : ""}`, "merge", "确认合并");
    } catch (error) { if (form.isConnected) { const message = form.querySelector("p"); if (message) message.textContent = error.name === "AbortError" ? "可选对话读取超时，请取消后重试。" : error.message; } }
    finally { clearTimeout(timeout); if (state.lanes.get("candidates") === controller) state.lanes.delete("candidates"); }
  }
}
function payloadFromForm(form) {
  const data = new FormData(form), operation = form.dataset.operation;
  const value = (key) => String(data.get(key) || "").trim();
  if (["rename", "rename_topic", "split_topic"].includes(operation) && !value("title")) throw new Error("名称不能为空，请填写后再保存。");
  if (operation === "edit_utterance" && !value("text")) throw new Error("转写文字不能为空；如需暂不显示，可使用隐藏发言。");
  if (operation === "rename") return { title: value("title") };
  if (operation === "edit_utterance") return { utterance_id: value("utterance_id"), text: value("text") };
  if (operation === "hide_utterance") return { utterance_id: value("utterance_id"), hidden: value("hidden") === "true" };
  if (operation === "split") return { at_utterance_id: value("at_utterance_id") };
  if (operation === "merge") {
    const option = form.querySelector('[name="other_id"]')?.selectedOptions[0];
    if (!option) throw new Error("请选择要合并的对话。");
    return { other_id: value("other_id"), other_revision: parseRevision(option.dataset.revision) };
  }
  if (operation === "rename_topic") return { topic_id: value("topic_id"), title: value("title") };
  if (operation === "merge_topics") {
    const ids = data.getAll("topic_ids");
    if (ids.length < 2) throw new Error("请选择至少两个要合并的主题。");
    return { topic_ids: ids };
  }
  if (operation === "split_topic") {
    const ids = data.getAll("utterance_ids"), total = number(form.querySelector("fieldset")?.dataset.total);
    if (!ids.length || ids.length >= total) throw new Error("请选择部分发言，并在原主题中至少保留一段。");
    return { topic_id: value("topic_id"), utterance_ids: ids, title: value("title") };
  }
  return {};
}
function parseRevision(value) { return /^\d+$/.test(String(value)) ? Number(value) : value; }
async function sessionToken(signal) {
  if (!state.csrf) {
    const session = await requestJSON("/api/session", { signal });
    if (!session.csrf) throw new Error("本机会话验证未就绪，请刷新页面后重试。");
    state.csrf = session.csrf;
  }
  return state.csrf;
}
async function submitCorrection(form) {
  if (state.editing) return;
  const errorBox = $("edit-error");
  errorBox.hidden = true;
  let payload;
  try { payload = payloadFromForm(form); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; return; }
  const controller = startLane("mutation"), timeout = setTimeout(() => controller.abort("timeout"), 20000);
  const id = form.dataset.conversationId;
  state.editing = true;
  form.querySelectorAll("button,input,textarea,select").forEach((element) => { element.disabled = true; });
  $("detail-close").disabled = true;
  try {
    const csrf = await sessionToken(controller.signal);
    const result = await requestJSON("/api/corrections", { method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ conversation_id: id, base_revision: parseRevision(form.dataset.revision), operation: form.dataset.operation, payload }) });
    if (!result.ok) throw new Error("服务未确认保存成功。请重新载入详情核对后再操作。");
    state.editing = false;
    toast("整理已保存为新版本，可在整理与修订中撤销。");
    await openDetail(result.id || id, "conversation");
    refresh({ manual: true });
  } catch (error) {
    if (error.status === 401 || error.status === 403) state.csrf = null;
    errorBox.hidden = false;
    errorBox.textContent = controller.signal.aborted ? "请求超时，暂时无法确认是否保存。请先重新载入详情核对，避免重复提交。" : error.message;
    if (error.status === 409 || controller.signal.aborted) {
      const reload = document.createElement("button");
      reload.type = "button"; reload.className = "text-button"; reload.dataset.action = "reload-detail"; reload.textContent = "重新载入详情";
      errorBox.append(document.createElement("br"), reload);
      // Keep this stale form from submitting again; its input remains available for review.
      form.dataset.stale = "true";
    }
  } finally {
    clearTimeout(timeout);
    state.editing = false;
    $("detail-close").disabled = false;
    if (form.isConnected) { form.querySelectorAll("button,input,textarea,select").forEach((element) => { element.disabled = false; }); if (form.dataset.stale) form.querySelector('[type="submit"]').disabled = true; }
    if (state.lanes.get("mutation") === controller) state.lanes.delete("mutation");
  }
}
function reprocessSourceKey(conversation) {
  return JSON.stringify(sortedChunks(conversation).map((chunk) => [chunk.chunk_id, chunk.sha256 || "", number(chunk.duration)]));
}
function canSubmitLocalReprocess(conversation, result) {
  return result.local_only === true && result.estimated_max_usd === 0 &&
    conversation.revision != null && list(conversation.chunks).length > 0;
}
function reprocessPreviewMarkup(conversation, result) {
  const chunks = sortedChunks(conversation);
  const allowed = canSubmitLocalReprocess(conversation, result);
  const total = chunks.reduce((sum, chunk) => sum + number(chunk.duration), 0);
  return `<form class="edit-form" id="reprocess-form"><h4>确认本地重算范围</h4>
    <p>${esc(titleOf(conversation))}</p>
    <p class="edit-hint">涉及 ${chunks.length} 段完整原音，文件时长合计 ${esc(duration(total))}。重新分析作用于关联原音的完整文件，可能包含本对话范围以外的内容。</p>
    <p>${esc(result.message || "尚无处理范围说明。")}</p>
    <p class="edit-hint">原始录音和人工修订保留。当前回听音源与位置不变，新处理结果完成后可重新载入。</p>
    ${allowed ? '<p class="edit-hint">仅本地分析，不调用云服务；预计云费用 $0。本次预览尚未创建任务。</p><div class="form-actions"><button class="primary-button" type="submit">确认仅本地重算</button><button class="secondary-button" type="button" data-action="cancel-edit">取消</button></div>' : '<p class="notice warning">尚未确认可仅在本地重算，当前不提供提交入口。涉及云处理的范围与预算需要另外核实。</p><button class="secondary-button" type="button" data-action="cancel-edit">关闭预览</button>'}
    <div id="reprocess-error" class="edit-error" role="alert" hidden></div></form>`;
}
async function previewReprocess() {
  if (!state.detail || state.editing || state.lanes.has("preview")) return;
  const conversation = state.detail, area = $("edit-area");
  if (!area) return;
  state.reprocessPreview = null;
  area.innerHTML = '<div class="notice">正在读取重算范围与费用估计；尚未启动重算。</div>';
  area.scrollIntoView({ block: "center" });
  const controller = startLane("preview"), timeout = setTimeout(() => controller.abort("timeout"), 18000);
  try {
    const csrf = await sessionToken(controller.signal);
    const result = await requestJSON("/api/reprocess/preview", { method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ conversation_id: conversation.id }) });
    if (controller.signal.aborted || state.detail !== conversation || !area.isConnected) return;
    if (canSubmitLocalReprocess(conversation, result)) {
      state.reprocessPreview = { conversationId: conversation.id, revision: conversation.revision, sourceKey: reprocessSourceKey(conversation), localOnly: true };
    }
    area.innerHTML = reprocessPreviewMarkup(conversation, result);
    area.querySelector("button")?.focus({ preventScroll: true });
  } catch (error) {
    if (controller.signal.aborted && controller.signal.reason !== "timeout") return;
    if (area.isConnected && state.detail === conversation) { area.innerHTML = '<div class="notice warning"></div>'; area.firstElementChild.textContent = error.name === "AbortError" ? "预览读取超时，未启动重算。" : error.message; }
  }
  finally { clearTimeout(timeout); if (state.lanes.get("preview") === controller) state.lanes.delete("preview"); }
}
async function submitReprocess(form) {
  const preview = state.reprocessPreview, errorBox = $("reprocess-error");
  if (state.editing || state.lanes.has("mutation") || !preview || !preview.localOnly ||
      !state.detail || state.detail.id !== preview.conversationId || form.dataset.stale) return;
  const controller = startLane("mutation"), timeout = setTimeout(() => controller.abort("timeout"), 20000);
  let submitted = false, succeeded = false;
  state.editing = true;
  errorBox.hidden = true;
  form.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  $("detail-close").disabled = true;
  try {
    const csrf = await sessionToken(controller.signal);
    const fresh = await requestJSON(`/api/conversations/${encodeURIComponent(preview.conversationId)}`, { signal: controller.signal });
    if (String(fresh.revision) !== String(preview.revision) || reprocessSourceKey(fresh) !== preview.sourceKey) {
      const conflict = new Error("对话版本或关联原音已有变化，本次未提交重算。请重新载入详情并查看新的重算范围。");
      conflict.status = 409;
      throw conflict;
    }
    submitted = true;
    const result = await requestJSON("/api/reprocess", {
      method: "POST", signal: controller.signal,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ conversation_id: preview.conversationId, base_revision: preview.revision, confirm: true, local_only: true })
    });
    if (result.ok !== true || result.local_only !== true) throw new Error("服务未确认仅本地重算的提交结果。请先查看处理状态，避免重复提交。");
    succeeded = true;
    state.reprocessPreview = null;
    form.innerHTML = '<h4>已提交本地重算</h4><p>关联原音已排入本地分析队列，完成后可重新载入对话。原音、人工修订及当前回听保持不变。</p><div class="form-actions"><button class="secondary-button" type="button" data-action="cancel-edit">关闭</button></div>';
    toast("本地重算已入队；可在处理状态中查看进度。");
    refresh({ manual: true });
  } catch (error) {
    if (error.status === 401 || error.status === 403) state.csrf = null;
    errorBox.hidden = false;
    errorBox.textContent = controller.signal.aborted ? (submitted ? "请求超时，无法确认是否已入队。请先查看处理状态，避免重复提交。" : "提交前的范围核对超时，尚未发送重算请求。请重新预览范围。") : submitted && !error.status ? "无法确认重算请求是否已入队。请先查看处理状态，核对后再重新预览，避免重复提交。" : error.message;
    // Neither an ambiguous POST nor a stale preview can be retried automatically.
    state.reprocessPreview = null;
    form.dataset.stale = "true";
    const reload = document.createElement("button");
    reload.type = "button"; reload.className = "text-button";
    reload.dataset.action = "reload-detail"; reload.textContent = "重新载入详情";
    errorBox.append(document.createElement("br"), reload);
  } finally {
    clearTimeout(timeout);
    state.editing = false;
    $("detail-close").disabled = false;
    if (form.isConnected) {
      form.querySelectorAll("button").forEach((button) => { button.disabled = false; });
      const submit = form.querySelector('[type="submit"]');
      if (submit && (form.dataset.stale || succeeded)) submit.disabled = true;
    }
    if (state.lanes.get("mutation") === controller) state.lanes.delete("mutation");
  }
}

// Delegated controls keep card markup free from inline event handlers.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action, id = button.dataset.id;
  if (action === "open-conversation" || action === "play-conversation") openDetail(id, "conversation", action === "play-conversation");
  else if (action === "open-chunk" || action === "play-chunk") openDetail(id, "chunk", action === "play-chunk");
  else if (action === "filter-topic") { state.topic = button.dataset.topic; $("topic").value = state.topic; refresh({ changed: true }); }
  else if (action === "play-detail" && state.detail) startPlayback(state.detail);
  else if (action === "play-full" && state.detail) startFullPlayback(state.detail);
  else if (action === "play-raw-detail" && state.detail) playRawChunk(state.detail);
  else if (action === "evidence") { try { revealEvidence(list(JSON.parse(button.dataset.ids)).map(String)); } catch { toast("无法读取这条证据引用。"); } }
  else if (action === "reload-detail" && state.detail) openDetail(state.detail.id, "conversation");
  else if (action === "cancel-edit") { if (state.editing) return; state.reprocessPreview = null; state.lanes.get("candidates")?.abort(); state.lanes.get("preview")?.abort(); if ($("edit-area")) $("edit-area").replaceChildren(); $("detail-title").focus(); }
  else if (action === "preview-reprocess") previewReprocess();
  else beginEdit(action, id);
});
$("detail-body").addEventListener("submit", (event) => {
  if (!["correction-form", "reprocess-form"].includes(event.target.id)) return;
  event.preventDefault();
  if (event.target.dataset.stale) return;
  if (event.target.id === "reprocess-form") submitReprocess(event.target);
  else submitCorrection(event.target);
});
$("detail-body").addEventListener("change", (event) => {
  if (event.target.id === "show-hidden") document.querySelectorAll("[data-hidden-utterance]").forEach((element) => { element.hidden = !event.target.checked; });
});
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("date-strip").addEventListener("click", (event) => { const button = event.target.closest("[data-day]"); if (button) setDate(button.dataset.day); });
$("date").addEventListener("change", (event) => { if (event.target.value) setDate(event.target.value); });
$("today").addEventListener("click", () => setDate(state.overview?.today || dateOf()));
function shiftDate(offset) { const day = new Date(`${state.date}T12:00:00+08:00`); day.setTime(day.getTime() + offset * 86400000); setDate(dateOf(day)); }
$("previous-day").addEventListener("click", () => shiftDate(-1));
$("next-day").addEventListener("click", () => shiftDate(1));
let searchTimer;
$("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const query = event.target.value.trim();
  searchTimer = setTimeout(() => { state.q = query; refresh({ changed: true }); }, 350);
});
$("topic").addEventListener("change", (event) => { state.topic = event.target.value; refresh({ changed: true }); });
$("refresh").addEventListener("click", () => refresh({ manual: true }));
$("load-more").addEventListener("click", () => { if (state.cursor && state.snapshotKey === currentKey()) refresh({ manual: true, append: true }); });
window.addEventListener("online", () => refresh());
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
state.date = dateOf();
$("date").value = state.date;
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
