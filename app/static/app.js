const $ = (id) => document.getElementById(id);

// escape user-controlled strings before interpolating into innerHTML
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let clipFiles = [];
let currentJobId = null;   // the job whose SSE stream we're attached to

// ---- populate presets + style packs ------------------------------------------
let PACKS = {};            // built-ins, keyed by key
let SAVED = {};            // user-saved extracted styles, keyed by id
let overlaySkin = "digicam";
fetch("/api/presets").then(r => r.json()).then(({ presets, packs, saved }) => {
  const sel = $("preset");
  presets.forEach(p => {
    const o = document.createElement("option");
    o.value = p.key; o.textContent = p.name;
    sel.appendChild(o);
  });
  (packs || []).forEach(p => { PACKS[p.key] = p; addPackOption(p.key, p.name); });
  (saved || []).forEach(p => { SAVED[p.key] = p; addPackOption(p.key, "★ " + p.name); });
});

function addPackOption(key, label) {
  const o = document.createElement("option");
  o.value = key; o.textContent = label;
  $("pack").appendChild(o);
  return o;
}

// picking a pack sets grade + skin + effect toggles + drama (still overridable).
// A SAVED pack also restores its extracted grade + reference stats, so the
// closed-loop tuner re-fits it to whatever footage you're rendering now.
$("pack").addEventListener("change", () => {
  const key = $("pack").value;
  const p = SAVED[key] || PACKS[key];
  if (!p) return;
  const isSaved = !!SAVED[key];

  refReq++; refAnalyzing = false;       // cancel any in-flight reference analysis
  $("refInput").value = "";
  $("refHint").classList.add("hidden");
  $("saveRow").classList.add("hidden");
  $("delPack").classList.toggle("hidden", !isSaved);

  refStyle = (isSaved && p.custom_grade && p.ref_stats)
    ? { custom_grade: p.custom_grade, ref_stats: p.ref_stats }
    : null;
  $("preset").value = p.preset;
  overlaySkin = p.overlay_skin;
  $("cpb").value = String(p.cuts_per_beat ?? $("cpb").value);
  $("flash").checked = p.flash;
  $("glitch").checked = p.glitch;
  $("zoom").checked = p.zoom_transitions;
  $("ramps").checked = p.speed_ramps;
  $("drama").value = Math.round(p.effect_intensity * 100);
  $("drama").dispatchEvent(new Event("input"));
});

// ---- save / delete extracted styles ------------------------------------------
async function readJsonResponse(response) {
  try {
    return await response.json();
  } catch (_) {
    return null;
  }
}

function packSaveError(response, data) {
  if (response.status === 400) {
    return "the server rejected invalid style data — your analyzed style is still here; " +
      "analyze the reference reel again if retrying doesn't work";
  }
  const detail = data && typeof data.detail === "string" ? data.detail : "";
  return detail || `save failed (${response.status})`;
}

$("savePack").addEventListener("click", async () => {
  if (!refStyle) return;
  const btn = $("savePack");
  const name = $("packName").value.trim() || "My style";
  btn.disabled = true;
  try {
    const pack = {
      ...refStyle,
      preset: $("preset").value,
      overlay_skin: overlaySkin,
      cuts_per_beat: parseFloat($("cpb").value),
      flash: $("flash").checked,
      glitch: $("glitch").checked,
      zoom_transitions: $("zoom").checked,
      speed_ramps: $("ramps").checked,
      effect_intensity: parseInt($("drama").value, 10) / 100,
    };
    const r = await fetch("/api/packs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, pack }),
    });
    // A response body is a one-shot stream. Read it exactly once so a valid
    // save cannot turn into "body stream already read" in the success path.
    const data = await readJsonResponse(r);
    if (!r.ok) throw new Error(packSaveError(r, data));
    const saved = data && data.pack;
    if (!saved || typeof saved !== "object" || !saved.key) {
      throw new Error("the server returned an invalid save response — your analyzed style is still here; retry");
    }
    SAVED[saved.key] = saved;
    pendingExtract = null;              // it's persisted now
    addPackOption(saved.key, "★ " + saved.name);
    $("pack").value = saved.key;          // select it without re-firing change
    $("delPack").classList.remove("hidden");
    $("saveRow").classList.add("hidden");
    $("packName").value = "";
    $("refHint").textContent = `saved as “${saved.name}” — it's in the Style pack list now`;
  } catch (e) {
    $("refHint").classList.remove("hidden");
    $("saveRow").classList.remove("hidden");
    if (!$("packName").value.trim()) $("packName").value = name;
    $("refHint").textContent = "couldn't save this pack: " + e.message;
  } finally {
    btn.disabled = false;
  }
});

$("delPack").addEventListener("click", async () => {
  const key = $("pack").value;
  if (!SAVED[key]) return;
  const name = SAVED[key].name;
  const r = await fetch(`/api/packs/${encodeURIComponent(key)}`, { method: "DELETE" });
  if (!r.ok) return;
  delete SAVED[key];
  [...$("pack").options].find(o => o.value === key)?.remove();
  $("delPack").classList.add("hidden");
  $("pack").value = "digicam";
  $("pack").dispatchEvent(new Event("change"));   // clears refStyle/pending UI
  if (pendingExtract) {
    // a slot just freed up — bring the unsaved extraction back so it can be
    // saved without re-analyzing the reference reel
    refStyle = pendingExtract.style;
    $("saveRow").classList.remove("hidden");
    $("refHint").classList.remove("hidden");
    $("refHint").textContent =
      `deleted “${name}” — your extracted style is still here, name it and save`;
  }
});

// ---- style-from-reference ----------------------------------------------------
let refStyle = null;   // {custom_grade, ref_stats} once a reel is analyzed
let refReq = 0;        // request sequencing (stale responses must not win)
let refAnalyzing = false;
// last extraction, kept alive across pack selection so that deleting a pack to
// free a slot (at the save limit) doesn't force a costly re-analysis
let pendingExtract = null;

$("refInput").addEventListener("change", async () => {
  const f = $("refInput").files[0];
  const prev = refStyle;               // keep the old style if this one fails
  const my = ++refReq;
  const hint = $("refHint");
  if (!f) { refStyle = null; hint.classList.add("hidden"); return; }
  refAnalyzing = true;
  hint.classList.remove("hidden");
  hint.textContent = "analyzing reference reel…";
  const fd = new FormData();
  fd.append("reference", f);
  try {
    const r = await fetch("/api/analyze-style", { method: "POST", body: fd });
    if (my !== refReq) return;         // a newer reel was selected meanwhile
    if (!r.ok) throw new Error(((await r.json()) || {}).detail || "failed");
    const { pack } = await r.json();
    refStyle = { custom_grade: pack.custom_grade, ref_stats: pack.stats };
    pendingExtract = { style: refStyle, name: "" };
    // apply the extracted pack to the visible controls
    $("cpb").value = String(pack.cuts_per_beat);
    $("flash").checked = pack.flash;
    $("glitch").checked = pack.glitch;
    $("zoom").checked = pack.zoom_transitions;
    $("ramps").checked = pack.speed_ramps;
    $("drama").value = Math.round(pack.effect_intensity * 100);
    $("drama").dispatchEvent(new Event("input"));
    const g = pack.custom_grade, s = pack.stats;
    hint.textContent =
      `extracted: ${s.n_cuts} cuts @ ${s.cut_rate}/s → ${pack.cuts_per_beat} cut/beat · ` +
      `sat ${g.saturation} · warm ${g.warmth} · grain ${g.grain} — ` +
      `grade fine-tunes to your footage at render`;
    // offer to keep it: a saved pack re-applies this style to future renders
    $("packName").value = (f.name || "").replace(/\.[^.]+$/, "").slice(0, 40);
    $("saveRow").classList.remove("hidden");
    $("delPack").classList.add("hidden");
  } catch (e) {
    if (my !== refReq) return;
    refStyle = prev;                   // failed analysis keeps the prior style
    hint.textContent = prev
      ? "couldn't analyze that reel — kept the previous reference style"
      : "couldn't analyze that reel — using the selected pack instead";
  } finally {
    if (my === refReq) refAnalyzing = false;
  }
});

// choosing a grade preset manually also exits reference-style mode (same
// contract as picking a built-in pack — custom_grade would silently win)
$("preset").addEventListener("change", () => {
  if (!refStyle && !refAnalyzing) return;
  refStyle = null; refReq++; refAnalyzing = false;
  $("refInput").value = "";
  $("refHint").classList.add("hidden");
  $("saveRow").classList.add("hidden");
  $("delPack").classList.add("hidden");
});

// ---- dropzone ----------------------------------------------------------------
const dz = $("dropzone");
const clipInput = $("clipInput");

dz.addEventListener("click", (e) => {
  if (e.target.tagName !== "LI" && !e.target.closest("li")) clipInput.click();
});
["dragenter", "dragover"].forEach(ev =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach(ev =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => addClips(e.dataTransfer.files));
clipInput.addEventListener("change", () => addClips(clipInput.files));

function addClips(fileList) {
  for (const f of fileList) {
    if (f.type.startsWith("video") || /\.(mp4|mov|m4v|avi|mkv|webm|hevc)$/i.test(f.name)) {
      clipFiles.push(f);
    }
  }
  renderClipList();
}

function renderClipList() {
  const ul = $("clipList");
  ul.innerHTML = "";
  clipFiles.forEach((f, i) => {
    const li = document.createElement("li");
    const mb = (f.size / 1048576).toFixed(1);
    li.innerHTML = `<b>${esc(f.name)}</b><span>${mb} MB &times;</span>`;
    li.querySelector("span").onclick = (e) => { e.stopPropagation(); clipFiles.splice(i, 1); renderClipList(); };
    ul.appendChild(li);
  });
}

// ---- duration label ----------------------------------------------------------
let reanalyzeTimer = null;
$("duration").addEventListener("input", (e) => {
  $("durVal").textContent = e.target.value + "s";
  if (musicToken) { clearTimeout(reanalyzeTimer); reanalyzeTimer = setTimeout(reanalyze, 450); }
});

// ---- music section analyzer --------------------------------------------------
let musicToken = null, musicAnalysis = null, musicStart = 0, musicReq = 0;

function fmt(t) { const m = Math.floor(t / 60), s = Math.round(t % 60); return `${m}:${String(s).padStart(2, "0")}`; }

$("musicInput").addEventListener("change", () => {
  const f = $("musicInput").files[0];
  musicToken = null; musicAnalysis = null; musicStart = 0;
  if (!f) { $("musicPanel").classList.add("hidden"); return; }
  analyzeMusic(f);
});

async function analyzeMusic(file) {
  const my = ++musicReq;
  $("musicPanel").classList.remove("hidden");
  $("chips").innerHTML = "";
  $("musicHint").textContent = "analyzing track…";
  const fd = new FormData();
  fd.append("music", file);
  fd.append("target", $("duration").value);
  try {
    const r = await fetch("/api/analyze-music", { method: "POST", body: fd });
    if (!r.ok) throw new Error(((await r.json()) || {}).detail || "failed");
    const { token, analysis } = await r.json();
    if (my !== musicReq) return;   // a newer track was selected meanwhile
    musicToken = token; musicAnalysis = analysis;
    if (analysis.sections.length) musicStart = analysis.sections[0].start;
    renderSections();
    const bpm = analysis.tempo ? `${Math.round(analysis.tempo)} BPM · ` : "";
    $("musicHint").textContent = `${bpm}click a suggestion or the waveform to set where the song starts`;
  } catch (e) {
    musicToken = null;
    $("musicHint").textContent = "couldn't analyze this track — it'll just play from the start";
  }
}

async function reanalyze() {
  if (!musicToken) return;
  const my = ++musicReq;
  try {
    const r = await fetch(`/api/music-sections?token=${encodeURIComponent(musicToken)}&target=${$("duration").value}`);
    if (!r.ok || my !== musicReq) return;   // ignore stale/superseded responses
    const { analysis } = await r.json();
    musicAnalysis = analysis;
    const maxStart = Math.max(0, analysis.duration - parseFloat($("duration").value));
    musicStart = Math.max(0, Math.min(musicStart, maxStart));
    renderSections();
  } catch (e) { /* keep prior suggestions */ }
}

function renderSections() {
  const a = musicAnalysis; if (!a) return;
  const box = $("chips"); box.innerHTML = "";
  a.sections.forEach(sec => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip" + (Math.abs(sec.start - musicStart) < 0.05 ? " active" : "");
    b.innerHTML = `${sec.label} · ${sec.descriptor}<span class="t">${fmt(sec.start)}</span>`;
    b.onclick = () => { musicStart = sec.start; renderSections(); };
    box.appendChild(b);
  });
  drawWave();
}

function drawWave() {
  const a = musicAnalysis; if (!a) return;
  const cv = $("wave"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 560, h = 64;
  cv.width = w * dpr; cv.height = h * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const env = a.envelope, n = env.length, bw = w / n;
  const target = parseFloat($("duration").value), dur = a.duration;
  const x0 = (musicStart / dur) * w, x1 = Math.min(w, ((musicStart + target) / dur) * w);
  ctx.fillStyle = "rgba(255,74,74,.13)"; ctx.fillRect(x0, 0, x1 - x0, h);
  for (let i = 0; i < n; i++) {
    const t = (i / n) * dur, inWin = t >= musicStart && t <= musicStart + target;
    const bh = Math.max(1, env[i] * (h - 6));
    ctx.fillStyle = inWin ? "#ff5a4d" : "#3a3f4a";
    ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 0.5), bh);
  }
  a.sections.forEach(sec => { ctx.fillStyle = "#ffd24d"; ctx.fillRect((sec.start / dur) * w - 1, 0, 2, h); });
  $("startLabel").textContent = `start ${fmt(musicStart)} · ${target}s`;
}

$("wave").addEventListener("click", (e) => {
  const a = musicAnalysis; if (!a) return;
  const rect = e.target.getBoundingClientRect();
  const frac = (e.clientX - rect.left) / rect.width;
  const target = parseFloat($("duration").value);
  musicStart = Math.max(0, Math.min(frac * a.duration, Math.max(0, a.duration - target)));
  renderSections();
});

// ---- effects drama label -----------------------------------------------------
function dramaLabel(v) {
  if (v < 45) return "Subtle" + (v < 45 ? " (no whips)" : "");
  if (v < 70) return "Gentle";
  if (v <= 95) return "Balanced";
  if (v <= 120) return "Punchy";
  return "Max";
}
$("drama").addEventListener("input", (e) => {
  $("dramaVal").textContent = dramaLabel(parseInt(e.target.value, 10));
});

// ---- generate ----------------------------------------------------------------
$("generate").addEventListener("click", async () => {
  if (!clipFiles.length) { showError("Add at least one clip first."); return; }
  if (refAnalyzing) {
    showError("Still analyzing the reference reel — try again in a moment.");
    return;
  }

  const cfg = {
    preset: $("preset").value,
    fill: $("fill").value,
    aspect: $("aspect").value,
    fps: parseInt($("fps").value || "30", 10),
    location: $("location").value,
    day: parseInt($("day").value || "0", 10),
    target_duration: parseFloat($("duration").value),
    cuts_per_beat: parseFloat($("cpb").value),
    overlay: $("overlay").checked,
    flash: $("flash").checked,
    glitch: $("glitch").checked,
    film_grain: $("grain").checked,
    vhs_jitter: $("jitter").checked,
    light_leak: $("leak").checked,
    zoom_transitions: $("zoom").checked,
    speed_ramps: $("ramps").checked,
    effect_intensity: parseInt($("drama").value, 10) / 100,
    overlay_skin: overlaySkin,
    music_start: musicToken ? musicStart : 0,
    keep_source_audio: $("srcAudio").checked,
    duck_threshold: parseFloat($("duckThreshold").value) || 0.05,
    duck_ratio: parseFloat($("duckRatio").value) || 8,
    auto_captions: $("autoCaptions").checked,
    caption: $("caption").value.trim(),
    quality: $("quality").value,
    hw_accel: $("hwaccel").checked,
    storyboard: $("sb").checked,
  };
  if (refStyle) {
    cfg.custom_grade = refStyle.custom_grade;
    cfg.ref_stats = refStyle.ref_stats;
  }
  resetStoryboard();

  const fd = new FormData();
  clipFiles.forEach(f => fd.append("clips", f));
  const music = $("musicInput").files[0];
  if (musicToken) fd.append("music_token", musicToken);   // reuse staged file
  else if (music) fd.append("music", music);
  fd.append("config", JSON.stringify(cfg));

  setBusy(true);
  hideError();
  setProgress("uploading…", 0);

  let res;
  try {
    res = await fetch("/api/jobs", { method: "POST", body: fd });
  } catch (err) { showError("Upload failed: " + err); setBusy(false); return; }
  if (!res.ok) { showError((await res.json()).detail || "Failed to start"); setBusy(false); return; }

  const { job_id } = await res.json();
  listen(job_id);
});

function listen(jobId) {
  currentJobId = jobId;
  $("cancelBtn").classList.remove("hidden");
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (e) => {
    const s = JSON.parse(e.data);
    if (s.status === "running" || s.status === "queued") {
      $("cancelBtn").classList.remove("hidden");
    }
    setProgress(labelFor(s.stage), s.pct);
    if (s.status === "storyboard") {
      es.close(); setBusy(false); hideCancel();
      loadStoryboard(jobId);
      refreshJobs();
    } else if (s.status === "done") {
      es.close(); setBusy(false); hideCancel();
      showResult(`/api/jobs/${jobId}/output.mp4`);
      refreshJobs();
      if (sb.jobId === jobId) {
        // re-fetch the plan: the storyboard now reflects the edits that were
        // rendered (reorder + trims are baked into the server's plan)
        loadStoryboard(jobId);
        const parts = [];
        if (s.rendered) parts.push(`rendered ${s.rendered} segment${s.rendered === 1 ? "" : "s"}`);
        if (s.reused) parts.push(`reused ${s.reused} unchanged`);
        $("sbHint").textContent = parts.join(" · ") + " — edits are now baked in";
        $("sbRender").disabled = false;
        $("sbRender").textContent = "Render again";
      }
    } else if (s.status === "cancelled") {
      es.close(); setBusy(false); hideCancel();
      showError("Render cancelled");
      refreshJobs();
    } else if (s.status === "error") {
      es.close(); setBusy(false); hideCancel();
      showError(s.error || "Render failed");
      refreshJobs();
    }
  };
  es.onerror = () => { es.close(); setBusy(false); hideCancel(); refreshJobs(); };
}

function hideCancel() { $("cancelBtn").classList.add("hidden"); }

$("cancelBtn").addEventListener("click", async () => {
  if (!currentJobId) return;
  $("cancelBtn").disabled = true;
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
  } catch (err) { /* the SSE stream will reflect reality either way */ }
  $("cancelBtn").disabled = false;
});

// ---- storyboard --------------------------------------------------------------
const sb = {
  jobId: null, slots: [], bench: [],
  order: [],            // original slot indices in display order
  swaps: new Map(),     // orig index -> bench index
  trims: new Map(),     // orig index -> start delta (s)
  caps: new Map(),      // orig index -> per-slot caption override
  sel: null,            // selected orig index (for bench swap / caption)
  past: [], future: [], // undo/redo history of edit state
};

function sbSnap() {
  return { order: [...sb.order], swaps: new Map(sb.swaps),
           trims: new Map(sb.trims), caps: new Map(sb.caps) };
}
function sbRestore(s) {
  sb.order = s.order.slice(); sb.swaps = new Map(s.swaps);
  sb.trims = new Map(s.trims); sb.caps = new Map(s.caps);
}
function sbPush() {
  sb.past.push(sbSnap());
  if (sb.past.length > 50) sb.past.shift();
  sb.future.length = 0;
}
function sbUndo() {
  if (!sb.past.length) return;
  sb.future.push(sbSnap()); sbRestore(sb.past.pop()); paintStoryboard();
}
function sbRedo() {
  if (!sb.future.length) return;
  sb.past.push(sbSnap()); sbRestore(sb.future.pop()); paintStoryboard();
}

function resetStoryboard() {
  sb.jobId = null; sb.slots = []; sb.bench = [];
  sb.order = []; sb.swaps.clear(); sb.trims.clear(); sb.caps.clear(); sb.sel = null;
  sb.past = []; sb.future = [];
  $("sbPlan").removeAttribute("href");
  $("sbCapRow").classList.add("hidden");
  $("storyboard").classList.add("hidden");
  $("sbRender").disabled = false;
  $("sbRender").textContent = "Render reel";
  $("sbHint").textContent = "";
}

async function loadStoryboard(jobId) {
  const r = await fetch(`/api/jobs/${jobId}/storyboard`);
  if (!r.ok) { showError("couldn't load storyboard"); return; }
  const data = await r.json();
  sb.jobId = jobId; sb.slots = data.slots; sb.bench = data.bench;
  $("sbPlan").href = `/api/jobs/${jobId}/plan.json`;
  sb.order = data.slots.map((_, i) => i);   // server order is the base order
  sb.swaps.clear(); sb.trims.clear(); sb.caps.clear(); sb.sel = null;
  sb.past = []; sb.future = [];
  data.slots.forEach((s, i) => { if (s.caption) sb.caps.set(i, s.caption); });
  $("sbTitle").textContent =
    `Storyboard · ${data.slots.length} shots · ${Math.round(data.target)}s · ${Math.round(data.tempo)} BPM`;
  $("storyboard").classList.remove("hidden");
  setProgress("review picks", 0.42);
  paintStoryboard();
}

const FX_SHORT = { blackzoom: "black", whipzoom: "whipz", zoom: "zoom",
  rampin: "ramp", rampout: "ramp", ramphold: "slow-mo",
  whipin_l: "whip", whipin_r: "whip", whipout_l: "whip", whipout_r: "whip" };

function trimDelta(o) { return Math.round((sb.trims.get(o) || 0) * 100) / 100; }

function moveShot(o, dir) {
  const i = sb.order.indexOf(o);
  const j = i + dir;
  if (j < 0 || j >= sb.order.length) return;
  sbPush();
  [sb.order[i], sb.order[j]] = [sb.order[j], sb.order[i]];
  paintStoryboard();
}

function paintStoryboard() {
  const fs = $("filmstrip"); fs.innerHTML = "";
  const nChanged = sb.swaps.size + sb.trims.size + sb.caps.size +
    (sb.order.some((o, i) => o !== i) ? 1 : 0);
  sb.order.forEach((o, pos) => {
    const s = sb.slots[o];
    const d = document.createElement("div");
    d.className = "sb-tile" + (sb.sel === o ? " sel" : "") +
                  (sb.swaps.has(o) || sb.trims.has(o) || sb.caps.has(o) ? " swapped" : "");
    const swap = sb.swaps.get(o);
    const thumb = swap != null ? (sb.bench[swap].thumb || s.thumb) : s.thumb;
    const fx = [...new Set(s.effects.map(e => FX_SHORT[e] || e))].join("·");
    const dayBadge = s.day > 0 ? `<span class="d">D${s.day}</span>` : "";
    const capBadge = sb.caps.has(o) ? `<span class="cap">cc</span>` : "";
    const trim = trimDelta(o);
    d.innerHTML =
      `<img src="${thumb}" loading="lazy"><span class="n">${pos + 1}</span>` +
      `${dayBadge}${capBadge}` + (fx ? `<span class="fx">${fx}</span>` : "") +
      `<span class="sb-ctl">` +
        `<button type="button" class="sb-btn" data-a="move" data-d="-1" title="move earlier">⇦</button>` +
        `<button type="button" class="sb-btn" data-a="move" data-d="1" title="move later">⇨</button>` +
        `<button type="button" class="sb-btn" data-a="trim" data-d="-1" title="start −0.25s">◀</button>` +
        `<button type="button" class="sb-btn" data-a="trim" data-d="1" title="start +0.25s">▶</button>` +
      `</span>` +
      (trim ? `<span class="trim-tag">${trim > 0 ? "+" : ""}${trim}s</span>` : "");
    d.onclick = () => {
      if (sb.swaps.has(o)) { sbPush(); sb.swaps.delete(o); sb.sel = null; }
      else sb.sel = (sb.sel === o ? null : o);
      paintStoryboard();
    };    d.title = `${s.src} @ ${s.start}s` +
      (sb.swaps.has(o) ? " (click to undo swap)" : "");
    d.querySelectorAll(".sb-btn").forEach(btn => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const dir = parseInt(btn.dataset.d, 10);
        if (btn.dataset.a === "move") moveShot(o, dir);
        else {
          sbPush();
          sb.trims.set(o, Math.round(((sb.trims.get(o) || 0) + dir * 0.25) * 100) / 100);
          paintStoryboard();
        }
      });
    });
    fs.appendChild(d);
  });
  $("sbCapRow").classList.toggle("hidden", sb.sel == null);
  if (sb.sel != null) $("sbCapInput").value = sb.caps.get(sb.sel) || "";
  const be = $("bench"); be.innerHTML = "";
  const usedBench = new Set(sb.swaps.values());
  sb.bench.forEach(b => {
    const d = document.createElement("div");
    d.className = "sb-tile" + (usedBench.has(b.index) ? " used" : "");
    d.innerHTML = `<img src="${b.thumb}" loading="lazy">`;
    d.title = `${b.src} @ ${b.center}s`;
    d.onclick = () => {
      if (sb.sel == null || usedBench.has(b.index)) return;
      sbPush();
      sb.swaps.set(sb.sel, b.index);
      sb.sel = null;
      paintStoryboard();
    };
    be.appendChild(d);
  });
  $("sbHint").textContent = nChanged
    ? `${nChanged} slot${nChanged === 1 ? "" : "s"} edited — only changed slots re-render`
    : (sb.sel != null ? `slot ${sb.sel + 1} selected — set a caption, or pick a bench shot to swap in` : "");
}

$("sbCapSet").addEventListener("click", () => {
  if (sb.sel == null) return;
  sbPush();
  const v = $("sbCapInput").value.trim();
  if (v) sb.caps.set(sb.sel, v); else sb.caps.delete(sb.sel);
  sb.sel = null;
  paintStoryboard();
});
$("sbCapClear").addEventListener("click", () => {
  if (sb.sel == null) return;
  sbPush();
  sb.caps.delete(sb.sel);
  sb.sel = null;
  paintStoryboard();
});
$("sbUndo").addEventListener("click", sbUndo);
$("sbRedo").addEventListener("click", sbRedo);

$("sbRender").addEventListener("click", async () => {
  if (!sb.jobId) return;
  const edits = { order: [...sb.order] };
  if (sb.swaps.size) {
    edits.swaps = [...sb.swaps.entries()].map(([slot, bench_index]) => ({ slot, bench_index }));
  }
  if (sb.trims.size) {
    edits.trims = [...sb.trims.entries()].map(([slot, delta]) => ({ slot, delta }));
  }
  if (sb.caps.size) {
    edits.captions = [...sb.caps.entries()].map(([slot, caption]) => ({ slot, caption }));
  }
  $("sbRender").disabled = true;
  setBusy(true); hideError();
  const r = await fetch(`/api/jobs/${sb.jobId}/render`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edits }),
  });
  if (!r.ok) {
    showError((await r.json()).detail || "render failed");
    setBusy(false); $("sbRender").disabled = false;
    return;
  }
  listen(sb.jobId);
  refreshJobs();
});

// ---- jobs panel ---------------------------------------------------------------
const STATUS_LABEL = {
  queued: "queued", running: "running", storyboard: "reviewing picks",
  done: "done", error: "failed", cancelled: "cancelled",
};

async function refreshJobs() {
  try {
    const r = await fetch("/api/jobs");
    if (!r.ok) return;
    renderJobs((await r.json()).jobs || []);
  } catch (err) { /* panel is best-effort */ }
}

function renderJobs(jobs) {
  const ul = $("jobsList");
  ul.innerHTML = "";
  if (!jobs.length) {
    ul.innerHTML = "<li class='job-empty'>no jobs yet — generate a reel to see it here</li>";
    return;
  }
  jobs.forEach(j => {
    const li = document.createElement("li");
    li.className = "job-row " + j.status;
    const dl = j.output
      ? `<a class="job-dl" href="${j.output}" download>⬇</a>` : "";
    const cancel = (j.status === "queued" || j.status === "running")
      ? `<button type="button" class="job-cancel" data-id="${j.id}">cancel</button>` : "";
    const err = j.error ? `<span class="job-err" title="${esc(j.error)}">!</span>` : "";
    li.innerHTML =
      `<span class="job-id">${j.id}</span>` +
      `<span class="job-status">${STATUS_LABEL[j.status] || j.status}</span>` +
      `<span class="job-stage">${j.status === "done" ? `${j.rendered} rendered · ${j.reused} reused` : esc(j.stage)}</span>` +
      `<span class="job-pct">${Math.round(j.pct * 100)}%</span>${err}${cancel}${dl}`;
    ul.appendChild(li);
  });
  ul.querySelectorAll(".job-cancel").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`/api/jobs/${btn.dataset.id}/cancel`, { method: "POST" });
      setTimeout(refreshJobs, 400);
    });
  });
}

$("jobsRefresh").addEventListener("click", refreshJobs);
refreshJobs();

const STAGE_LABELS = {
  probe: "reading clips", beats: "finding the beat", analyze: "scoring moments",
  select: "picking best shots", render: "cutting to beat", assemble: "assembling",
  effects: "grading + overlay", sourceaudio: "mixing original audio",
  captions: "adding captions", mux: "adding music", done: "done", uploading: "uploading",
};
function labelFor(stage) { return STAGE_LABELS[stage] || stage; }

function setProgress(label, pct) {
  $("progress").classList.remove("hidden");
  $("barFill").style.width = Math.round(pct * 100) + "%";
  $("stageLabel").textContent = `${label} — ${Math.round(pct * 100)}%`;
}
function showResult(url) {
  const v = $("player");
  v.src = url + "?t=" + Date.now();
  v.load();
  $("previewHint").classList.add("hidden");
  const dl = $("download");
  dl.href = url; dl.classList.remove("hidden");
  const sh = $("share");
  sh.classList.remove("hidden");
  sh.onclick = () => shareReel(url);
  setProgress("done", 1);
}

async function shareReel(url) {
  // Web Share API when available; otherwise copy the link to the clipboard.
  const absolute = new URL(url, location.href).href;
  if (navigator.share) {
    try {
      await navigator.share({ title: "Reel Clipper", url: absolute });
      return;
    } catch (e) { if (e.name === "AbortError") return; /* fall through */ }
  }
  try {
    await navigator.clipboard.writeText(absolute);
    $("share").textContent = "✓ Link copied";
    setTimeout(() => { $("share").textContent = "↗ Share"; }, 1500);
  } catch (e) { /* clipboard blocked — nothing else to do */ }
}
function setBusy(b) { $("generate").disabled = b; $("generate").textContent = b ? "Working…" : "Generate Reel"; }
function showError(msg) { const e = $("error"); e.textContent = msg; e.classList.remove("hidden"); }
function hideError() { $("error").classList.add("hidden"); }
