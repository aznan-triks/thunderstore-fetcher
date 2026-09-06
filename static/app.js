// ── Thunderstore Archive — single-page app (SPA) ────────────────────────────────
// Modeled on the wiki-archive interface: sidebar (job list) + detail panel
// filled in dynamically, with no page reload.

// Pipeline steps — fallback only; the live steps (labels + markers) come from
// each job's /api/jobs meta ("steps" field, single source of truth in
// pipeline.py). Kept in sync by selectJob().
const STEPS = [
  { id: "fetch",   label: "Fetching",   marker: "[1/6]" },
  { id: "extract", label: "Extraction", marker: "[2/6]" },
  { id: "group",   label: "Grouping",   marker: "[3/6]" },
  { id: "build",   label: "Building",   marker: "[4/6]" },
  { id: "export",  label: "Export",     marker: "[5/6]" },
  { id: "copy",    label: "Done",       marker: "[6/6]" },
];

const STATUS_LABEL = {
  pending: "Pending", running: "Running", paused: "Paused",
  stopped: "Stopped", done: "Done",       error: "Error",
};

// Terminal line coloring
const LOG_RULES = [
  [t => t.includes("━━━") || t.includes("════") || /\[\d\/6\]/.test(t), "l-sep"],
  [t => t.startsWith("✓") || t.includes("DONE"),                        "l-ok"],
  [t => t.startsWith("✗") || /\bERROR\b|Error|Traceback/i.test(t),      "l-err"],
  [t => t.startsWith("⚠") || /warning/i.test(t),                        "l-warn"],
  [t => t.startsWith("⏸") || t.startsWith("⏹"),                         "l-dim"],
];

// ── global state ─────────────────────────────────────────────────────────────
let allJobs     = {};
let selectedId  = null;
let logStream   = null;
let currentStep = -1;
let creatingNew = false;
let currentSteps = STEPS;  // steps of the selected job (server meta) or fallback

// creation form state
let currentTags = [];
let blacklist   = new Set();

// ── startup ──────────────────────────────────────────────────────────────────
document.getElementById("btn-new").addEventListener("click", newJob);
refresh();
setInterval(refresh, 2000);

// ── polling ──────────────────────────────────────────────────────────────────
async function refresh() {
  let list;
  try {
    list = await fetch("/api/jobs").then(r => r.json());
  } catch (err) {
    console.error("Failed to poll /api/jobs:", err);
    list = [];
  }
  allJobs = Object.fromEntries(list.map(j => [j.id, j]));
  renderSidebar();
  if (selectedId && allJobs[selectedId]) updateDetail(allJobs[selectedId]);
}

// ── sidebar ──────────────────────────────────────────────────────────────────
function renderSidebar() {
  const el = document.getElementById("job-list");
  const jobs = Object.values(allJobs).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  if (!jobs.length && !creatingNew) {
    el.innerHTML = '<div class="job-empty">No jobs</div>';
    return;
  }
  el.innerHTML = jobs.map(j => `
    <div class="job-item${j.id === selectedId ? " active" : ""}" data-id="${j.id}">
      <div class="job-item-top">
        <div class="job-dot dot-${j.status}"></div>
        <div class="job-name" title="${esc(j.community)}">${esc(j.community)}</div>
      </div>
      <div class="job-meta">${STATUS_LABEL[j.status] || j.status} · ${relDate(j.created_at)}</div>
    </div>`).join("");
  el.querySelectorAll(".job-item").forEach(it =>
    it.addEventListener("click", () => selectJob(it.dataset.id)));
}

function relDate(iso) {
  if (!iso) return "";
  const sec = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (sec < 60)    return "just now";
  if (sec < 3600)  return Math.floor(sec / 60) + "min";
  if (sec < 86400) return Math.floor(sec / 3600) + "h";
  return Math.floor(sec / 86400) + "d";
}

// ── job creation (form in the detail panel) ────────────────────────────────────
function newJob() {
  creatingNew = true;
  selectedId  = null;
  stopLogStream();
  renderSidebar();
  blacklist = new Set();
  currentTags = [];
  document.getElementById("detail").innerHTML = `
    <div class="card">
      <div class="card-title">New job</div>

      <label>Community (game)</label>
      <select id="community-select"><option value="">⏳ Loading…</option></select>

      <div class="section-title">
        Categories to exclude
        <span id="saved-badge" class="saved-badge">✓ Saved</span>
      </div>
      <div id="tags-container"><p class="tags-hint">Select a community…</p></div>

      <div class="section-title">Export formats <span class="hint">(at least one)</span></div>
      <div class="fmt-row">
        <label class="opt-check"><input type="checkbox" class="f-fmt" value="pdf"><span>📄 PDF</span></label>
        <label class="opt-check"><input type="checkbox" class="f-fmt" value="md"><span>📝 Markdown</span></label>
        <label class="opt-check"><input type="checkbox" class="f-fmt" value="txt"><span>📃 TXT</span></label>
      </div>

      <div class="section-title">Settings</div>
      <div class="opts-grid">
        <div>
          <label>API workers <span class="hint">(parallel)</span></label>
          <input type="number" id="f-workers-api" value="4" min="1" max="16">
        </div>
        <div>
          <label>Delay between requests (s)</label>
          <input type="number" id="f-delay" value="0.2" step="0.1" min="0.1" max="5">
        </div>
        <div>
          <label>Max words / file <span class="hint">(splitting)</span></label>
          <input type="number" id="f-maxwords" value="200000" min="1000" step="1000">
        </div>
        <div>
          <label>Max files / group <span class="hint">(Free:50 · Pro:300)</span></label>
          <input type="number" id="f-maxgroups" value="99" min="1" max="500">
        </div>
        <div>
          <label>PDF workers</label>
          <input type="number" id="f-workers-pdf" value="2" min="1" max="8">
        </div>
        <div>
          <label>Max size / PDF (MB)</label>
          <input type="number" id="f-maxsize" value="100" min="10" max="500">
        </div>
      </div>
      <label class="opt-check">
        <input type="checkbox" id="f-skip"><span>Skip mods with no README</span>
      </label>
      <label class="opt-check">
        <input type="checkbox" id="f-clean" checked><span>Start from scratch <span class="hint">(clears previous output)</span></span>
      </label>

      <div class="actions" style="margin-top:1.2rem">
        <button class="btn btn-primary" id="btn-submit" disabled>🚀 Create and start</button>
        <button class="btn btn-ghost" id="btn-save" disabled>💾 Save config</button>
        <button class="btn btn-ghost" id="btn-cancel">Cancel</button>
      </div>
    </div>`;

  document.getElementById("btn-submit").addEventListener("click", submitJob);
  document.getElementById("btn-save").addEventListener("click", saveCurrentConfig);
  document.getElementById("btn-cancel").addEventListener("click", cancelNew);
  document.getElementById("community-select").addEventListener("change", onCommunityChange);
  document.querySelectorAll(".f-fmt").forEach(c => c.addEventListener("change", updateFormState));
  populateCommunities();
}

function cancelNew() {
  creatingNew = false;
  renderSidebar();
  document.getElementById("detail").innerHTML = `
    <div class="empty-state">
      <h2>No job selected</h2>
      <p>Create a new job or select one from the list.</p>
    </div>`;
}

async function populateCommunities() {
  const select = document.getElementById("community-select");
  try {
    const data = await fetch("/api/communities").then(r => { if (!r.ok) throw 0; return r.json(); });
    if (!data.length) { select.innerHTML = '<option value="">⚠️ No community</option>'; return; }
    select.innerHTML = data.map(c => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`).join("");
    onCommunityChange();
  } catch (err) {
    console.error("Failed to load communities:", err);
    select.innerHTML = '<option value="">❌ Unable to load</option>';
  }
}

// The "Create" button requires a community AND at least one checked format.
// "Save config" only requires a community.
function updateFormState() {
  const community = document.getElementById("community-select").value;
  const submit = document.getElementById("btn-submit");
  const save   = document.getElementById("btn-save");
  if (submit) submit.disabled = !community || selectedFormats().length === 0;
  if (save)   save.disabled   = !community;
}

async function onCommunityChange() {
  const community = document.getElementById("community-select").value;
  const cont   = document.getElementById("tags-container");
  updateFormState();
  if (!community) return;

  cont.innerHTML = '<p class="tags-hint">Loading categories…</p>';
  blacklist = new Set();

  const fail = (what) => (err) => {
    console.error(`Failed to load ${what}:`, err);
    return what === "categories" ? [] : {};
  };
  const [cats, saved] = await Promise.all([
    fetch(`/api/communities/${community}/categories`).then(r => r.ok ? r.json() : []).catch(fail("categories")),
    fetch(`/api/configs/${community}`).then(r => r.ok ? r.json() : {}).catch(fail("config")),
  ]);
  currentTags = cats;
  if (saved && Object.keys(saved).length) applyConfig(saved);
  renderTags();
  updateFormState();
}

function applyConfig(cfg) {
  const set = (id, v) => { if (v != null) document.getElementById(id).value = v; };
  set("f-workers-api", cfg.workers_api);
  set("f-delay",       cfg.delay);
  set("f-workers-pdf", cfg.workers_pdf);
  set("f-maxsize",     cfg.max_size_mb);
  set("f-maxgroups",   cfg.max_groups);
  set("f-maxwords",    cfg.max_words_per_file);
  if (Array.isArray(cfg.export_formats)) {
    const want = new Set(cfg.export_formats);
    document.querySelectorAll(".f-fmt").forEach(c => { c.checked = want.has(c.value); });
  }
  if (cfg.skip_empty_readme != null) document.getElementById("f-skip").checked = !!cfg.skip_empty_readme;
  if (cfg.clean_start != null) document.getElementById("f-clean").checked = !!cfg.clean_start;
  if (Array.isArray(cfg.blacklist_categories)) blacklist = new Set(cfg.blacklist_categories);
}

function renderTags() {
  const cont = document.getElementById("tags-container");
  if (!currentTags.length) { cont.innerHTML = '<p class="tags-hint">No categories available.</p>'; return; }
  const grid = document.createElement("div");
  grid.className = "tag-grid";
  [...currentTags].sort().forEach(cat => {
    const chip = document.createElement("span");
    const on = blacklist.has(cat);
    chip.className = "tag-chip" + (on ? " active" : "");
    chip.textContent = (on ? "✕ " : "") + cat;
    chip.addEventListener("click", () => toggleTag(cat, chip));
    grid.appendChild(chip);
  });
  cont.innerHTML = "";
  cont.appendChild(grid);
}

function toggleTag(cat, chip) {
  if (blacklist.has(cat)) { blacklist.delete(cat); chip.className = "tag-chip"; chip.textContent = cat; }
  else                    { blacklist.add(cat);    chip.className = "tag-chip active"; chip.textContent = "✕ " + cat; }
}

function selectedFormats() {
  return [...document.querySelectorAll(".f-fmt:checked")].map(c => c.value);
}

function buildConfig() {
  const num = id => +document.getElementById(id).value;
  return {
    workers_api:          num("f-workers-api"),
    delay:                num("f-delay"),
    workers_pdf:          num("f-workers-pdf"),
    max_size_mb:          num("f-maxsize"),
    max_groups:           num("f-maxgroups"),
    max_words_per_file:   num("f-maxwords"),
    export_formats:       selectedFormats(),
    blacklist_categories: [...blacklist],
    skip_empty_readme:    document.getElementById("f-skip").checked,
    clean_start:          document.getElementById("f-clean").checked,
  };
}

async function saveCurrentConfig() {
  const community = document.getElementById("community-select").value;
  if (!community) return;
  try {
    const res = await fetch(`/api/configs/${community}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildConfig()),
    });
    if (!res.ok) { toast("Save failed: " + await res.text(), "err"); return; }
  } catch (err) {
    toast("Network error: " + err.message, "err");
    return;
  }
  const badge = document.getElementById("saved-badge");
  badge.classList.add("show");
  setTimeout(() => badge.classList.remove("show"), 2000);
}

async function submitJob() {
  const community = document.getElementById("community-select").value;
  if (!community) { alert("Community required"); return; }
  if (selectedFormats().length === 0) { alert("Choose at least one export format"); return; }
  const btn = document.getElementById("btn-submit");
  btn.disabled = true; btn.textContent = "⏳ Creating…";
  try {
    const res = await fetch("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ community, config: JSON.stringify(buildConfig()) }),
    });
    if (!res.ok) { alert("Error: " + await res.text()); btn.disabled = false; btn.textContent = "🚀 Create and start"; return; }
    const job = await res.json();
    allJobs[job.id] = job;
    await fetch(`/api/jobs/${job.id}/start`, { method: "POST" });
    creatingNew = false;
    selectJob(job.id);
    refresh();
  } catch (err) {
    alert("Network error: " + err.message);
    btn.disabled = false; btn.textContent = "🚀 Create and start";
  }
}

// ── selection & detail rendering ────────────────────────────────────────────────
function selectJob(id) {
  selectedId  = id;
  currentStep = -1;
  creatingNew = false;
  stopLogStream();
  const job = allJobs[id];
  if (!job) return;
  // Steps come from the server meta (single source of truth in pipeline.py);
  // the local constant is only a fallback for jobs that predate it.
  currentSteps = (Array.isArray(job.steps) && job.steps.length) ? job.steps : STEPS;
  renderFullDetail(job);
  renderSidebar();
  startLogStream(id, 0);
}

function renderFullDetail(job) {
  document.getElementById("detail").innerHTML = `
    <div class="detail-head">
      <div style="flex:1">
        <div class="detail-title">
          <span class="name">${esc(job.community)}</span>
          <span id="d-badge" class="badge st-${job.status}">${badgeHtml(job.status)}</span>
          <button class="btn btn-ghost btn-sm" id="d-delete" style="margin-left:auto" title="Delete">🗑</button>
        </div>
        <div class="detail-sub">${job.files.length} file(s) · created ${relDate(job.created_at)}</div>
      </div>
    </div>
    <div id="d-actions" class="actions">${buildActions(job)}</div>
    <div class="card">
      <div class="card-title">Progress</div>
      <div id="d-steps" class="steps">${buildSteps()}</div>
    </div>
    <div class="card">
      <div class="term-head">
        <div class="card-title" style="margin:0">Log</div>
        <button class="btn btn-ghost btn-sm" id="d-clear">Clear</button>
      </div>
      <div id="terminal"><span class="l-dim">Loading logs…</span></div>
    </div>
    <div id="d-files" class="card" style="${job.files.length ? "" : "display:none"}">
      <div class="term-head">
        <div class="card-title" style="margin:0">Generated files</div>
        <button class="btn btn-ghost btn-sm" id="d-reveal" title="Copy the folder path">📋 Copy path</button>
      </div>
      <div class="files-grid">${buildFiles(job)}</div>
    </div>`;
  document.getElementById("d-delete").addEventListener("click", () => deleteJob(job.id));
  document.getElementById("d-clear").addEventListener("click", () => { document.getElementById("terminal").innerHTML = ""; });
  document.getElementById("d-reveal").addEventListener("click", () => revealOutput(job.id));
  bindActions(job.id);
}

function updateDetail(job) {
  if (!selectedId || selectedId !== job.id) return;
  const badge = document.getElementById("d-badge");
  const acts  = document.getElementById("d-actions");
  const sub   = document.querySelector(".detail-sub");
  const files = document.getElementById("d-files");
  if (badge) { badge.className = `badge st-${job.status}`; badge.innerHTML = badgeHtml(job.status); }
  if (acts)  { acts.innerHTML = buildActions(job); bindActions(job.id); }
  if (sub)   sub.textContent = `${job.files.length} file(s) · created ${relDate(job.created_at)}`;
  if (job.status === "error" && currentStep >= 0) setStep(currentStep, "error");
  // Always refresh the files panel: a restart with a clean start empties the
  // output folder, and stale rows would keep pointing at deleted files.
  if (files) {
    files.style.display = job.files.length ? "" : "none";
    const grid = files.querySelector(".files-grid");
    if (grid) grid.innerHTML = buildFiles(job);
  }
}

function buildActions(job) {
  const s = job.status;
  const log = `<a class="btn btn-ghost btn-sm" href="/api/jobs/${job.id}/download-log">⬇ Logs</a>`;
  const dl  = `<a class="btn btn-ghost" href="/api/jobs/${job.id}/download-pdfs">📦 Download files</a>`;
  let main = "";
  if (s === "running")      main = `<button class="btn btn-warn" data-act="pause">⏸ Pause</button><button class="btn btn-danger" data-act="stop">⏹ Stop</button>`;
  else if (s === "paused")  main = `<button class="btn btn-primary" data-act="resume">▶ Resume</button><button class="btn btn-danger" data-act="stop">⏹ Stop</button>`;
  else if (s === "stopped" || s === "error") main = `<button class="btn btn-primary" data-act="resume">↩ Restart</button>`;
  else if (s === "done")    main = `<button class="btn btn-ghost" data-act="resume">↺ Restart</button>${dl}`;
  else                      main = `<button class="btn btn-primary" data-act="start">▶ Start</button>`;
  return main + log;
}

function bindActions(id) {
  document.querySelectorAll("#d-actions [data-act]").forEach(b =>
    b.addEventListener("click", () => jobAction(id, b.dataset.act)));
}

function buildSteps() {
  return currentSteps.map((s, i) => `
    <div class="step" id="step-${i}">
      <div class="step-dot">${i + 1}</div>
      <div class="step-lbl">${s.label}</div>
    </div>`).join("");
}

function buildFiles(job) {
  if (!job.files.length) return "";
  const total = job.files.reduce((s, f) => s + f.size_mb, 0).toFixed(1);
  const rows = job.files.map(f => `
    <div class="file-row">
      <span>📄</span>
      <span class="file-name" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="file-size">${f.size_mb} MB</span>
    </div>`).join("");
  return `
    <div class="files-summary">
      <span>📁 data/output/${esc(job.community)}/</span>
      <span>${job.files.length} file${job.files.length > 1 ? "s" : ""} · ${total} MB</span>
    </div>${rows}`;
}

function badgeHtml(status) {
  return (status === "running" ? '<span class="spin"></span> ' : "") + (STATUS_LABEL[status] || status);
}

// ── actions ────────────────────────────────────────────────────────────────────
async function jobAction(id, action) {
  await fetch(`/api/jobs/${id}/${action}`, { method: "POST" });
  if (action === "resume" || action === "start") {
    stopLogStream();
    currentStep = -1;
    const term = document.getElementById("terminal");
    if (term) term.innerHTML = "";
    startLogStream(id, 0);
  }
  refresh();
}

// Copies the output folder path to the clipboard.
async function revealOutput(id) {
  try {
    const res = await fetch(`/api/jobs/${id}/reveal`, { method: "POST" });
    if (!res.ok) { toast(await res.text() || "Folder unavailable", "err"); return; }
    const { path } = await res.json();
    try {
      await navigator.clipboard.writeText(path);
      toast("📋 Path copied: " + path);
    } catch {
      toast("📂 Location: " + path);
    }
  } catch (err) {
    toast("Network error: " + err.message, "err");
  }
}

// Small reusable ephemeral notification banner.
function toast(msg, kind) {
  let t = document.getElementById("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.className = "toast show" + (kind === "err" ? " toast-err" : "");
  t.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 4000);
}

async function deleteJob(id) {
  if (!confirm("Delete this job and all its data?")) return;
  let res;
  try {
    res = await fetch(`/api/jobs/${id}`, { method: "DELETE" });
  } catch (err) {
    toast("Network error: " + err.message, "err");
    return;
  }
  if (!res.ok) {
    // Previously the UI always announced "Job deleted" even when the request
    // failed, silently leaving the job in place.
    toast("Delete failed: " + (await res.text() || res.status), "err");
    return;
  }
  selectedId = null;
  stopLogStream();
  document.getElementById("detail").innerHTML = `
    <div class="empty-state"><h2>Job deleted</h2><p>Create a new job or select one.</p></div>`;
  refresh();
}

// ── log stream (replayed from from_pos, then followed live) ──────────────────────
function startLogStream(jobId, fromPos) {
  stopLogStream();
  const term = document.getElementById("terminal");
  if (!term) return;
  if (fromPos === 0) term.innerHTML = "";
  logStream = new EventSource(`/api/jobs/${jobId}/logs?from_pos=${fromPos}`);
  logStream.onmessage = (e) => {
    if (e.data === "__END__") { logStream.close(); return; }
    if (e.data) appendLog(e.data);
  };
  logStream.onerror = () => logStream && logStream.close();
}

function stopLogStream() {
  if (logStream) { logStream.close(); logStream = null; }
}

function appendLog(text) {
  const term = document.getElementById("terminal");
  if (!term) return;
  const div = document.createElement("div");
  for (const [test, cls] of LOG_RULES) if (test(text)) { div.className = cls; break; }
  div.textContent = text || " ";
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;

  const idx = currentSteps.findIndex(s => text.includes(s.marker));
  if (idx !== -1) { currentStep = idx; setStep(idx - 1, "done"); setStep(idx, "active"); }
  if (text.includes("DONE")) setStep(currentSteps.length, "done");
}

function setStep(active, state) {
  currentSteps.forEach((_, i) => {
    const el = document.getElementById(`step-${i}`);
    if (!el) return;
    el.classList.remove("active", "done", "error");
    if      (i < active)  el.classList.add("done");
    else if (i === active) el.classList.add(state);
  });
}

// ── utils ────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
