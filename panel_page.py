"""HTML-страница панели управления (без внешних зависимостей)."""

from __future__ import annotations

PANEL_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USV: камеры и запись</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #101418; color: #e6edf3; }
  header { padding: 12px 16px; border-bottom: 1px solid #2a333d;
           display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .badge { padding: 3px 9px; border-radius: 999px; background: #26313c;
           font-size: 13px; border: 1px solid #3a4754; }
  .badge[data-state="idle"] { background: #203040; }
  .badge[data-state="preview"] { background: #16402a; border-color: #2e7d4f; }
  .badge[data-state="starting"] { background: #3a3320; border-color: #8a6d2a; }
  .badge[data-state="recording"] { background: #4a1f1f; border-color: #b04444; }
  .badge[data-state="stopping"] { background: #3a3320; border-color: #8a6d2a; }
  .badge[data-state="error"] { background: #4a1f1f; border-color: #e05252; }
  main { padding: 12px; display: grid; gap: 12px; }
  .panel { border: 1px solid #2a333d; border-radius: 8px; background: #161b21;
           padding: 10px 12px; }
  .panel h2 { font-size: 15px; margin: 0 0 8px; font-weight: 600; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  button { background: #1e2b38; color: #e6edf3; border: 1px solid #3a5a74;
           border-radius: 7px; padding: 8px 12px; font-size: 14px; cursor: pointer; }
  button:hover:not(:disabled) { background: #25394b; }
  button.danger { background: #48201f; border-color: #8f3c3a; }
  button:disabled { opacity: 0.45; cursor: default; }
  select { max-width: 100%; background: #0d1115; color: #e6edf3;
           border: 1px solid #3a4754; border-radius: 6px; padding: 7px 8px; }
  label { display: flex; gap: 6px; align-items: center; font-size: 13px; }
  input { background: #0d1115; color: #e6edf3; border: 1px solid #3a4754;
          border-radius: 6px; padding: 7px 8px; font-size: 14px; }
  #message { color: #cfdae4; font-size: 14px; min-height: 1.3em; }
  #message.error { color: #ffb4ae; }
  .muted { color: #8b9bab; font-size: 12px; }
  .kv { display: grid; grid-template-columns: minmax(140px, auto) 1fr;
        gap: 3px 12px; font-size: 13px; }
  .kv div:nth-child(odd) { color: #8b9bab; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
          gap: 12px; }
  .cam { border: 1px solid #2a333d; border-radius: 8px; overflow: hidden;
         background: #161b21; display: flex; flex-direction: column; min-width: 0; }
  .cam-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
              padding: 8px 10px; border-bottom: 1px solid #2a333d; }
  .cam-title { font-weight: 600; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #7a8794; }
  .dot.live { background: #35c46a; }
  .dot.error { background: #e05252; }
  .dot.warn { background: #d8a13a; }
  .viewport { background: #000; min-height: 180px; display: flex;
              align-items: center; justify-content: center; }
  .viewport img { display: block; width: 100%; height: auto; max-height: 60vh;
                  object-fit: contain; }
  .details { padding: 7px 10px; font-size: 12px; color: #9fb0c0;
             display: flex; flex-direction: column; gap: 4px; }
  .cam-error { margin: 0 10px 8px; padding: 6px 8px; border-radius: 6px;
               background: #44201f; color: #ffc9c6; font-size: 12px; }
  .warning { color: #ffd48a; }
  pre { margin: 0; padding: 8px; background: #0d1115; border-radius: 6px;
        color: #b9c7d3; font-size: 12px; white-space: pre-wrap; }
  #token-box { display: none; }
  #token-box.visible { display: block; }
  footer { padding: 6px 16px 20px; color: #8b9bab; font-size: 12px; line-height: 1.5; }
</style>
</head>
<body>
<header>
  <h1>USV: камеры и запись</h1>
  <span id="state" class="badge" data-state="starting">загрузка</span>
  <span id="summary" class="muted"></span>
  <span class="spacer" style="flex:1"></span>
  <button id="token-toggle" type="button">Токен</button>
</header>
<main>
  <section class="panel" id="token-box">
    <h2>Токен управления</h2>
    <form id="token-form" class="row">
      <input id="token-input" type="password" autocomplete="off"
             placeholder="X-USV-Token" size="42">
      <button type="submit">Сохранить</button>
    </form>
    <p class="muted">Токен хранится в localStorage этого браузера и передаётся
      только в заголовке X-USV-Token. В URL он не попадает.</p>
  </section>

  <section class="panel">
    <h2>Управление</h2>
    <div class="row">
      <button id="preview-start" type="button">Включить просмотр обеих камер</button>
      <button id="preview-stop" type="button">Остановить просмотр</button>
      <button id="record-start" type="button" class="danger">Начать запись обеих камер</button>
      <button id="record-stop" type="button" class="danger">Остановить запись</button>
      <button id="error-ack" type="button">Сбросить ошибку</button>
    </div>
    <p id="message"></p>
    <p id="conflicts" class="warning"></p>
  </section>

  <section class="panel" id="profile-panel">
    <h2>Профиль обеих камер · MJPG</h2>
    <form id="profile-form" class="row">
      <label>Режим <select id="profile-mode" disabled></select></label>
      <label>Dynamic <select id="profile-dynamic" disabled>
        <option value="0">0 — не снижать частоту</option>
        <option value="1">1 — разрешить снижение частоты</option>
      </select></label>
      <label>Длительность <select id="profile-duration" disabled></select></label>
      <button id="profile-apply" type="submit" disabled>Применить профиль</button>
      <button id="profile-check" type="button" disabled>Проверить режимы</button>
    </form>
    <p id="profile-current" class="muted"></p>
    <p id="profile-support" class="muted"></p>
    <p id="profile-feedback" role="status" aria-live="polite"></p>
    <pre id="profile-reasons"></pre>
    <p class="muted">Изменять профиль можно только после остановки просмотра и записи.
      Применение сохраняет выбор в памяти панели, но не открывает камеры.
      Dynamic=1 разрешает, но не гарантирует снижение FPS или увеличение яркости.
      FPS передачи просмотра не равен FPS захвата. После перезапуска выбор сбрасывается.</p>
  </section>

  <section class="panel">
    <h2>Состояние</h2>
    <div class="kv" id="record-info"></div>
  </section>

  <section class="grid" id="grid"></section>

  <section class="panel">
    <h2>Итог последней записи</h2>
    <div class="kv" id="result"></div>
  </section>

  <section class="panel">
    <h2>Ручная диагностика</h2>
    <pre id="manual">Если панель показывает ошибку занятости, новые просмотр и запись
запрещены. Проверьте на Repka:

pgrep -af 'record_dual_session|record_camera_pyav_queue'
ps -fp &lt;PID&gt;
ls -l ~/.cache/usv_record/
tail -n 50 &lt;session&gt;/cam-left.log &lt;session&gt;/cam-right.log

Автоматическое завершение посторонних процессов панель не выполняет.</pre>
  </section>
</main>
<footer>
  Запись не зависит от браузера и Wi-Fi: закрытие страницы её не останавливает.
  После перезапуска панели запись автоматически не возобновляется.
  Аварийное падение панели не гарантирует сохранение записи.
</footer>

<script>
const CAMERAS = __CAMERAS_JSON__;
const TOKEN_KEY = "usvPanelToken";
let token = localStorage.getItem(TOKEN_KEY) || "";
let lastStatus = null;
let busy = false;
let profileDirty = false;
let profileServerKey = "";
let profileOptionsKey = "";
let profileDurationsKey = "";
const streams = {};
const cards = {};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  if (token) headers["X-USV-Token"] = token;
  return headers;
}

function setTokenVisible(visible) {
  document.getElementById("token-box").classList.toggle("visible", visible);
  if (visible) document.getElementById("token-input").focus();
}

document.getElementById("token-toggle").addEventListener("click", () => {
  setTokenVisible(!document.getElementById("token-box").classList.contains("visible"));
});
document.getElementById("token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  token = document.getElementById("token-input").value.trim();
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  setTokenVisible(!token);
  refresh();
});

function buildCards() {
  const grid = document.getElementById("grid");
  for (const cam of CAMERAS) {
    const card = el("article", "cam");
    const head = el("div", "cam-head");
    const dot = el("span", "dot");
    const title = el("span", "cam-title", cam.label);
    const sub = el("span", "muted", cam.device);
    const spacer = el("span", "");
    spacer.style.flex = "1";
    const diag = el("button", "", "Диагностический кадр");
    diag.type = "button";
    diag.addEventListener("click", () => diagnostic(cam.key));
    head.append(dot, title, sub, spacer, diag);
    const viewport = el("div", "viewport");
    const img = document.createElement("img");
    img.alt = cam.label;
    viewport.append(img);
    const details = el("div", "details");
    const error = el("div", "cam-error");
    error.hidden = true;
    card.append(head, viewport, details, error);
    grid.append(card);
    cards[cam.key] = {card, dot, img, details, error};
  }
}

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " КиБ";
  return (bytes / (1024 * 1024)).toFixed(1) + " МиБ";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) +
    ":" + String(s).padStart(2, "0");
}

function stateLabel(state) {
  const labels = {
    idle: "ожидание",
    preview: "просмотр",
    starting: "запуск",
    recording: "запись",
    stopping: "остановка и проверка",
    error: "ошибка",
  };
  return labels[state] || state;
}

async function api(path, options) {
  const request = Object.assign({cache: "no-store"}, options || {});
  request.headers = authHeaders(request.headers);
  const response = await fetch(path, request);
  if (response.status === 401) {
    setTokenVisible(true);
    throw new Error("требуется токен");
  }
  let body = null;
  try { body = await response.json(); } catch (error) { body = null; }
  if (!response.ok) {
    const message = body && body.message ? body.message : ("HTTP " + response.status);
    throw new Error(message);
  }
  return body;
}

async function post(command) {
  if (busy) return;
  busy = true;
  render();
  try {
    await api("/api/" + command, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
  } catch (error) {
    if (command.startsWith("profile/")) profileFeedback(error.message, true);
    else setMessage(error.message, true);
  } finally {
    busy = false;
    refresh();
  }
}

function profileReady(status) {
  if (!status || !status.profile) return false;
  const support = status.profile_support || {};
  return ["ok", "fake"].includes(support.state) &&
    (status.profile_options || []).some(
      (option) => option.mode === status.profile.mode && option.supported);
}

function profileFeedback(text, error) {
  const node = document.getElementById("profile-feedback");
  node.textContent = text || "";
  node.className = error ? "warning" : "muted";
}

function renderProfile(status) {
  const mode = document.getElementById("profile-mode");
  const dynamic = document.getElementById("profile-dynamic");
  const duration = document.getElementById("profile-duration");
  const profile = status && status.profile;
  const options = (status && status.profile_options) || [];
  const support = (status && status.profile_support) || {};
  const optionsKey = JSON.stringify(options);
  if (optionsKey !== profileOptionsKey) {
    const selected = mode.value;
    mode.textContent = "";
    for (const item of options) {
      const option = el("option", "", item.mode + (item.supported ? "" : " — не подтверждён"));
      option.value = item.mode;
      option.disabled = !item.supported;
      mode.append(option);
    }
    if (options.some((item) => item.mode === selected)) mode.value = selected;
    profileOptionsKey = optionsKey;
  }
  const durations = ((status && status.profile_durations) || []).slice();
  if (profile && !durations.includes(profile.duration_seconds)) durations.push(profile.duration_seconds);
  const durationsKey = JSON.stringify(durations);
  if (durationsKey !== profileDurationsKey) {
    const selected = duration.value;
    duration.textContent = "";
    for (const seconds of durations) {
      const option = el("option", "", seconds === 0 ? "До остановки" : fmtDuration(seconds));
      option.value = String(seconds);
      duration.append(option);
    }
    if (durations.some((seconds) => String(seconds) === selected)) duration.value = selected;
    profileDurationsKey = durationsKey;
  }
  const serverKey = JSON.stringify(profile);
  if (profile && (!profileDirty || serverKey !== profileServerKey)) {
    mode.value = profile.mode;
    dynamic.value = String(profile.exposure_dynamic_framerate);
    duration.value = String(profile.duration_seconds);
    if (profileDirty && serverKey !== profileServerKey) {
      profileFeedback("Профиль изменён в другой вкладке; показан текущий выбор.", false);
    }
    profileDirty = false;
  }
  profileServerKey = serverKey;
  const locked = !status || status.profile_locked || busy;
  for (const node of [mode, dynamic, duration]) node.disabled = locked;
  const selected = options.find((item) => item.mode === mode.value);
  document.getElementById("profile-apply").disabled = locked || !profileDirty ||
    !["ok", "fake"].includes(support.state) || !selected || !selected.supported;
  document.getElementById("profile-check").disabled = locked || support.state === "checking";
  document.getElementById("profile-current").textContent = profile ?
    "Применён: " + profile.mode + " · dynamic=" + profile.exposure_dynamic_framerate +
    " · предел: " + (profile.duration_seconds === 0 ? "до остановки" : fmtDuration(profile.duration_seconds)) +
    (profileDirty ? " · есть неприменённый выбор" : "") : "Профиль не получен";
  const labels = {unknown: "не проверена", checking: "идёт проверка", ok: "перечисление завершено",
                  failed: "проверка не удалась", fake: "FAKE — оборудование не проверялось"};
  document.getElementById("profile-support").textContent =
    "Поддержка: " + (labels[support.state] || "—") +
    (support.checked_at ? " · " + support.checked_at : "") +
    (support.error ? " · " + support.error : "") +
    (support.fake ? " · фиктивен только просмотр; кнопка записи не является симулятором" : "");
  document.getElementById("profile-reasons").textContent = options
    .filter((item) => !item.supported)
    .map((item) => item.mode + ": " + ((item.reasons || []).join("; ") || "поддержка не подтверждена"))
    .join("\\n");
}

for (const id of ["profile-mode", "profile-dynamic", "profile-duration"]) {
  document.getElementById(id).addEventListener("change", () => {
    profileDirty = true;
    profileFeedback("Выбор не применён. Нажмите «Применить профиль».", false);
    render();
  });
}
document.getElementById("profile-check").addEventListener("click", () => post("profile/check"));
document.getElementById("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (document.getElementById("profile-apply").disabled || busy) return;
  const payload = {
    mode: document.getElementById("profile-mode").value,
    exposure_dynamic_framerate: Number(document.getElementById("profile-dynamic").value),
    duration_seconds: Number(document.getElementById("profile-duration").value),
  };
  busy = true;
  render();
  try {
    await api("/api/profile/apply", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    profileDirty = false;
    profileFeedback("Профиль применён. Камеры не открывались.", false);
  } catch (error) {
    profileFeedback(error.message, true);
  } finally {
    busy = false;
    await refresh();
  }
});

function setMessage(text, isError) {
  const node = document.getElementById("message");
  node.textContent = text || "";
  node.className = isError ? "error" : "";
}

document.getElementById("preview-start").addEventListener("click",
  () => post("preview/start"));
document.getElementById("preview-stop").addEventListener("click",
  () => post("preview/stop"));
document.getElementById("record-start").addEventListener("click",
  () => post("record/start"));
document.getElementById("record-stop").addEventListener("click",
  () => post("record/stop"));
document.getElementById("error-ack").addEventListener("click",
  () => post("error/ack"));

function allowed(command, status) {
  if (!status || busy) return false;
  const state = status.state;
  if (command === "preview/start") return state === "idle" && profileReady(status) && !profileDirty && !status.profile_locked;
  if (command === "preview/stop") return state === "preview";
  if (command === "record/start") return ["idle", "preview"].includes(state) && profileReady(status) && !profileDirty && !(status.state === "idle" && status.profile_locked);
  if (command === "record/stop") return state === "recording" || (state === "starting" && status.operation === "record");
  if (command === "error/ack") return state === "error";
  return false;
}

function cameraStatus(status, key) {
  if (!status) return null;
  const recording = status.recording || {};
  const preview = status.preview || {};
  const useRecord = recording.active || status.state === "recording" ||
    status.state === "stopping";
  const table = useRecord ? recording.cameras : preview.cameras;
  return (table && table[key]) || null;
}

function renderCamera(cam, status) {
  const card = cards[cam.key];
  if (!card) return;
  const info = cameraStatus(status, cam.key);
  card.dot.className = "dot";
  card.error.hidden = true;
  if (!info) {
    card.details.textContent = "нет данных";
  } else if (status.state === "recording" || status.state === "stopping" ||
             (status.recording && status.recording.active)) {
    const lines = [];
    lines.push("метаданные: " + (info.status || "—"));
    const requested = info.requested_mode || {};
    const negotiated = info.negotiated_mode || {};
    lines.push("запрошено: " + (requested.width ? requested.width + "x" + requested.height + "@" + requested.fps : "—"));
    lines.push("открыто: " + (negotiated.width ? negotiated.codec + " " + negotiated.width + "x" + negotiated.height : "—"));
    lines.push("темп принятых пакетов: " + (info.measured_capture_fps == null ? "—" : info.measured_capture_fps) + " пак/с");
    lines.push("сегментов: " + (info.segments === undefined ? "—" : info.segments) +
               " · " + fmtBytes(info.bytes));
    if (info.last_change_age_s !== null && info.last_change_age_s !== undefined) {
      lines.push("последний рост: " + info.last_change_age_s.toFixed(0) + " с назад");
    }
    if (info.stop_reason) lines.push("причина: " + info.stop_reason);
    card.details.textContent = lines.join(" · ");
    if (info.error) {
      card.error.textContent = info.error;
      card.error.hidden = false;
      card.dot.classList.add("error");
    } else if (info.warning) {
      card.dot.classList.add("warn");
    } else if (info.status === "recording") {
      card.dot.classList.add("live");
    } else if (info.status === "failed") {
      card.dot.classList.add("error");
    }
  } else {
    card.details.textContent =
      "состояние: " + (info.state || info.status || "—") +
      " · кадров: " + (info.published_frames === undefined ? "—" : info.published_frames) +
      " · клиентов: " + (info.stream_clients === undefined ? "—" : info.stream_clients) +
      " · захват запрошен: " + (info.capture_fps_requested == null ? "—" : info.capture_fps_requested) + " FPS" +
      " · темп входа: " + (info.capture_fps_measured == null ? "—" : info.capture_fps_measured) + " пак/с" +
      " · публикация: " + (info.published_fps_measured == null ? "—" : info.published_fps_measured) + " кадр/с";
    if (info.error) {
      card.error.textContent = "Ошибка камеры: " + info.error;
      card.error.hidden = false;
      card.dot.classList.add("error");
    } else if (info.state === "running") {
      card.dot.classList.add("live");
    } else if (info.state === "error") {
      card.dot.classList.add("error");
    }
  }
  if (info && info.warning) {
    card.error.textContent = info.warning;
    card.error.hidden = false;
    card.dot.classList.add("warn");
  }
}

function renderRecordInfo(status) {
  const box = document.getElementById("record-info");
  box.textContent = "";
  const rows = [
    ["Состояние", stateLabel(status.state)],
    ["Сессия", status.recording.session || "—"],
    ["Длительность", fmtDuration(status.recording.elapsed_s)],
    ["Свободно", status.disk && status.disk.free_mib !== null ?
      status.disk.free_mib.toFixed(0) + " МиБ" : "—"],
    ["Порог запуска", status.limits.min_free_mib + " МиБ"],
    ["Критический резерв", status.limits.critical_free_mib + " МиБ"],
    ["Профиль записи", status.recording.profile ?
      status.recording.profile.mode + " · dynamic=" + status.recording.profile.exposure_dynamic_framerate : "—"],
    ["Предел записи", status.recording.profile ?
      (status.recording.profile.duration_seconds === 0 ? "до остановки" : fmtDuration(status.recording.profile.duration_seconds)) : "—"],
    ["Публикация просмотра", status.limits.preview_publish_fps + " кадр/с (не FPS захвата)"],
    ["Причина остановки", status.recording.stop_reason || "—"],
    ["Код wrapper", status.recording.exit_code === null ? "—" :
      String(status.recording.exit_code)],
    ["Последняя ошибка", status.last_error || "—"],
  ];
  if (status.disk && status.disk.error) {
    rows.push(["Ошибка disk_usage", status.disk.error]);
  }
  for (const [name, value] of rows) {
    box.append(el("div", "", name), el("div", "", String(value)));
  }
  document.getElementById("conflicts").textContent =
    status.conflicts && status.conflicts.length ?
      "Занятость: " + status.conflicts.join("; ") : "";
}

function renderResult(status) {
  const box = document.getElementById("result");
  box.textContent = "";
  const result = status.result;
  if (!result) {
    box.append(el("div", "", "нет завершённых записей"));
    return;
  }
  const verification = result.verification || {};
  const rows = [
    ["Итог", result.outcome || "—"],
    ["Проверка файлов", Object.keys(verification).map(
      (key) => key + ": " + verification[key]).join("; ") || "—"],
    ["Причина остановки", result.stop_reason || "—"],
    ["Принудительное завершение", result.forced ? "да" : "нет"],
    ["Код wrapper", result.exit_code === null ? "—" : String(result.exit_code)],
    ["Сессия", result.session || "—"],
    ["Ошибка", result.error || "—"],
  ];
  const cameras = (status.recording && status.recording.cameras) || {};
  for (const cam of CAMERAS) {
    const info = cameras[cam.key] || {};
    rows.push([cam.label + ": темп принятых пакетов",
      info.measured_capture_fps == null ? "— (ещё не рассчитан)" : info.measured_capture_fps + " пак/с"]);
  }
  for (const [name, value] of rows) {
    box.append(el("div", "", name), el("div", "", String(value)));
  }
}

function render() {
  const status = lastStatus;
  const state = status ? status.state : "starting";
  const badge = document.getElementById("state");
  badge.textContent = stateLabel(state);
  badge.dataset.state = state;
  document.getElementById("summary").textContent = status ?
    ("сессия: " + (status.recording.session || "—")) : "нет связи";
  setMessage(status ? status.message : "подключение…", !!(status && status.state === "error"));

  for (const command of ["preview/start", "preview/stop", "record/start",
                         "record/stop", "error/ack"]) {
    document.getElementById(command === "error/ack" ? "error-ack" :
      command.replace("/", "-")).disabled = !allowed(command, status);
  }
  renderProfile(status);
  if (status) {
    renderRecordInfo(status);
    renderResult(status);
    for (const cam of CAMERAS) renderCamera(cam, status);
  }

  if (status && status.state === "preview") {
    for (const cam of CAMERAS) startStream(cam.key);
  } else {
    for (const cam of CAMERAS) stopStream(cam.key);
  }
}

function findJpeg(data) {
  let start = -1;
  for (let i = 0; i + 1 < data.length; i++) {
    if (data[i] === 0xff && data[i + 1] === 0xd8) { start = i; break; }
  }
  if (start < 0) return null;
  for (let i = start + 2; i + 1 < data.length; i++) {
    if (data[i] === 0xff && data[i + 1] === 0xd9) {
      return {jpeg: data.slice(start, i + 2), consumed: i + 2};
    }
  }
  return null;
}

function concatBytes(a, b) {
  if (!a.length) return b;
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function stopStream(key) {
  const state = streams[key];
  if (!state) return;
  if (state.controller) state.controller.abort();
  if (state.url) URL.revokeObjectURL(state.url);
  state.controller = null;
  state.url = null;
  const card = cards[key];
  if (card) card.img.removeAttribute("src");
}

async function startStream(key) {
  const state = streams[key] || (streams[key] = {controller: null, url: null});
  if (state.controller) return;
  const controller = new AbortController();
  state.controller = controller;
  try {
    const response = await fetch("/stream/" + key + ".mjpg", {
      cache: "no-store",
      headers: authHeaders(),
      signal: controller.signal,
    });
    if (response.status === 401) {
      setTokenVisible(true);
      throw new Error("требуется токен");
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const reader = response.body.getReader();
    let buffer = new Uint8Array(0);
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer = concatBytes(buffer, chunk.value);
      if (buffer.length > 8 * 1024 * 1024) buffer = buffer.slice(-1024 * 1024);
      let frame;
      while ((frame = findJpeg(buffer))) {
        buffer = buffer.slice(frame.consumed);
        const card = cards[key];
        if (!card) continue;
        if (state.url) URL.revokeObjectURL(state.url);
        state.url = URL.createObjectURL(new Blob([frame.jpeg], {type: "image/jpeg"}));
        card.img.src = state.url;
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      setMessage("Поток " + key + ": " + error.message, true);
    }
  } finally {
    if (state.controller === controller) state.controller = null;
  }
}

async function diagnostic(key) {
  try {
    const response = await fetch("/diagnostics/frame.jpg?camera=" + encodeURIComponent(key), {
      cache: "no-store",
      headers: authHeaders(),
    });
    if (response.status === 401) {
      setTokenVisible(true);
      throw new Error("требуется токен");
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = key + "-diagnostic.jpg";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (error) {
    setMessage("Диагностический кадр: " + error.message, true);
  }
}

async function refresh() {
  if (!token) {
    setTokenVisible(true);
    return;
  }
  try {
    const status = await api("/api/status");
    lastStatus = status;
    setTokenVisible(false);
    render();
  } catch (error) {
    document.getElementById("summary").textContent = "нет связи";
    setMessage(error.message, true);
  }
}

buildCards();
for (const cam of CAMERAS) {
  streams[cam.key] = {controller: null, url: null};
}
setTokenVisible(!token);
refresh();
setInterval(refresh, 1000);
window.addEventListener("beforeunload", () => {
  for (const cam of CAMERAS) stopStream(cam.key);
});
</script>
</body>
</html>
"""
