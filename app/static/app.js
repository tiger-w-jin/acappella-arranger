"use strict";

/**
 * A Cappella Arranger — UI.
 *
 * The editing loop is: select bars on the timeline, click a style, hear the
 * result. Every change re-arranges automatically (debounced), so there is no
 * "generate" button to forget to press.
 */

const NOTE_NAMES = ["C", "C\u266f", "D", "D\u266f", "E", "F", "F\u266f", "G", "G\u266f", "A", "A\u266f", "B"];

// One hue per style, so the timeline shows the arrangement's shape at a glance.
const STYLE_COLOURS = {
  satb_chorale:  "#7c9cff",
  hymn_open:     "#5fb2e8",
  barbershop:    "#e8a33d",
  jazz_close:    "#c98bf0",
  gospel_pad:    "#f0785a",
  doo_wop:       "#5fd0b0",
  rhythmic_vamp: "#e86f9e",
  cluster:       "#9d8cf5",
  sus_air:       "#6fc6d8",
  open_fifths:   "#a8b0c4",
  pop_stack:     "#8fd35f",
  drone:         "#b98a6a",
  unison:        "#7d8496",
};
const FALLBACK_COLOUR = "#7d8496";

const ARRANGE_DEBOUNCE_MS = 450;
const LYRICS_DEBOUNCE_MS = 700;
const AUDIO_RE = /\.(wav|mp3|flac|ogg|m4a|aac|aiff|aif|wma|mp4|m4v|mov|webm|mkv|avi|flv|wmv|3gp)$/i;

// Loaded on demand the first time a PDF is exported, so the page does not pay
// for a PDF library it may never use.
const PDF_SCRIPTS = [
  "https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js",
  "https://cdn.jsdelivr.net/npm/svg2pdf.js@2.2.3/dist/svg2pdf.umd.min.js",
];
const MODE_KEY = "acappella.mode";
const PROJECT_KEY = "acappella.project";
const AUTOSAVE_MS = 1200;
const PROJECT_VERSION = 1;

const state = {
  mode: "simple",       // "simple" | "pro"
  pendingAudio: null,   // audio file waiting on its transcription settings
  soloVoice: null,      // voice index currently soloed for playback
  arrangementId: null,
  voices: [],
  autosaveTimer: null,
  undoSnapshot: null,   // restores the state before the last typed command
  catalog: null,
  session: null,
  /** @type {{index:number, chord:string, roman:string, melody:Array, style:string, chordOverride:string}[]} */
  bars: [],
  selected: new Set(),
  lastClickedBar: null,
  activeStyle: null,
  osmd: null,
  measureRects: [],     // pixel rect per bar in the rendered score
  barTimes: [],         // {start, end} seconds per bar
  arrangeTimer: null,
  arrangeAbort: null,
  arrangeSeq: 0,
  playing: false,
  duration: 0,
  currentBar: -1,
};

const $ = (id) => document.getElementById(id);
const colourFor = (styleId) => STYLE_COLOURS[styleId] || FALLBACK_COLOUR;

function styleName(styleId) {
  const found = state.catalog?.styles.find((s) => s.id === styleId);
  return found ? found.name : styleId;
}

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function setStatus(el, message, kind = "") {
  el.textContent = message;
  el.className = "status" + (kind ? ` ${kind}` : "");
  el.hidden = !message;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch { /* no JSON body */ }
    throw new Error(detail);
  }
  return response.json();
}

// ───────────────────────────────────────────────────────── catalog ──

async function loadCatalog() {
  state.catalog = await api("/api/catalog");

  const ensemble = $("ensemble");
  ensemble.innerHTML = "";
  for (const item of state.catalog.ensembles) {
    ensemble.append(new Option(`${item.name} — melody in ${item.melody_voice}`, item.id));
  }
  ensemble.value = "satb";

  const inspStyle = $("insp-style");
  inspStyle.innerHTML = "";
  for (const style of state.catalog.styles) {
    const option = new Option(style.name, style.id);
    option.title = style.description;
    inspStyle.append(option);
  }

  const transpose = $("transpose");
  transpose.innerHTML = "";
  for (let n = -12; n <= 12; n++) {
    transpose.append(new Option(
      n === 0 ? "Original key" : `${n > 0 ? "+" : ""}${n} semitone${Math.abs(n) === 1 ? "" : "s"}`,
      String(n),
    ));
  }
  transpose.value = "0";

  const langs = $("lyric-lang");
  langs.innerHTML = "";
  for (const item of state.catalog.lyric_languages || [{ id: "en", name: "English" }]) {
    langs.append(new Option(item.name, item.id));
  }
  langs.value = "en";

  $("accepted-types").textContent =
    `Scores: ${state.catalog.accepted_score.join(" ")} · Audio: ${state.catalog.accepted_audio.join(" ")}`;

  renderPalette();
  renderCommandExamples();
}

function renderPalette() {
  const palette = $("palette");
  palette.innerHTML = "";
  const counts = new Map();
  for (const bar of state.bars) counts.set(bar.style, (counts.get(bar.style) || 0) + 1);

  // In Simple mode the highlighted chip reflects the piece, not a click.
  const uniform = counts.size === 1 ? [...counts.keys()][0] : null;
  const highlighted = state.mode === "simple" ? uniform : state.activeStyle;

  for (const style of state.catalog.styles) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "style-chip" + (highlighted === style.id ? " active" : "");
    chip.style.setProperty("--swatch", colourFor(style.id));
    chip.title = style.description;
    chip.setAttribute("role", "option");
    chip.setAttribute("aria-selected", String(highlighted === style.id));

    const swatch = document.createElement("span");
    swatch.className = "swatch";
    const label = document.createElement("span");
    label.textContent = style.name;
    chip.append(swatch, label);

    const used = state.mode === "simple" ? 0 : counts.get(style.id);
    if (used) {
      const badge = document.createElement("span");
      badge.className = "count";
      badge.textContent = used;
      chip.append(badge);
    }

    chip.addEventListener("click", () => {
      state.activeStyle = style.id;
      showStyleDetail(style);
      if (state.mode === "simple") {
        // Simple mode has no bar selection: a style applies to the whole piece.
        applyStyleToBars(style.id, state.bars.map((b) => b.index));
      } else if (state.selected.size) {
        applyStyleToBars(style.id, [...state.selected]);
      } else {
        $("palette-hint").textContent = "Select bars below, then click a style to apply it.";
      }
      renderPalette();
    });
    palette.append(chip);
  }
}

function showStyleDetail(style) {
  $("sd-name").textContent = style.name;
  $("sd-description").textContent = style.description;
  $("sd-syllable").textContent = `sings “${style.syllable}”`;
  $("style-detail").hidden = false;
  $("sd-preview").dataset.style = style.id;
}

// ───────────────────────────────────────────────────────── upload ──

/**
 * Notated files go straight through. Audio pauses first: the time signature and
 * repeat-merging change what gets transcribed, and transcription is slow, so it
 * is worth showing those two settings at the one moment they matter.
 */
function chooseFile(file) {
  if (!file) return;
  if (AUDIO_RE.test(file.name)) {
    state.pendingAudio = file;
    $("audio-filename").textContent = file.name;
    $("audio-kind").textContent = /\.(mp4|m4v|mov|webm|mkv|avi|flv|wmv|3gp)$/i.test(file.name)
      ? "is a video. Its audio track will be pulled out and transcribed."
      : "is a recording. These decide how it gets transcribed.";
    $("audio-options").hidden = false;
    setStatus($("upload-status"), "");
    $("transcribe").focus();
    return;
  }
  $("audio-options").hidden = true;
  state.pendingAudio = null;
  uploadFile(file);
}

async function uploadFile(file) {
  if (!file) return;
  const isAudio = AUDIO_RE.test(file.name);
  $("dropzone").classList.add("busy");
  $("upload-progress").hidden = false;
  setStatus($("upload-status"),
    isAudio ? `Transcribing “${file.name}” — this can take up to a minute…`
            : `Reading “${file.name}”…`);

  const [beats, beatType] = $("time-signature").value.split("/");
  const form = new FormData();
  form.append("file", file);
  form.append("beats", beats);
  form.append("beat_type", beatType);
  form.append("chords_per_bar", $("chords-per-bar").value);
  form.append("merge_repeats", $("merge-repeats").checked ? "true" : "false");
  form.append("harmony_source", $("reinfer").checked ? "infer" : "auto");

  try {
    applyAnalysis(await api("/api/upload", { method: "POST", body: form }));
    setStatus($("upload-status"), "");
  } catch (error) {
    setStatus($("upload-status"), error.message, "error");
  } finally {
    $("dropzone").classList.remove("busy");
    $("upload-progress").hidden = true;
    $("audio-options").hidden = true;
    state.pendingAudio = null;
  }
}

function applyAnalysis(analysis) {
  state.session = analysis;
  state.selected.clear();
  state.lastClickedBar = null;
  state.currentBar = -1;
  state.bars = analysis.bars.map((bar) => ({
    index: bar.index,
    chord: bar.chord,
    roman: bar.roman,
    melody: bar.melody,
    style: state.catalog.default_style,
    chordOverride: "",
  }));

  $("piece-title").textContent = analysis.title;
  const source = analysis.source_kind === "audio" ? "from audio" : "from score";
  $("piece-meta").textContent =
    `${source} · ${analysis.key} · ${analysis.time_signature} · ${analysis.bar_count} bars`;
  $("tempo").value = Math.round(analysis.tempo);

  // Lyrics belong to the piece that was loaded, so a different piece starts
  // clean rather than inheriting words that no longer fit its melody.
  // loadProject() puts saved lyrics back after this runs.
  $("lyrics").value = analysis.source_lyrics || "";
  renderLyricStrip([]);

  const notices = [];
  if (analysis.transcription_note) notices.push(analysis.transcription_note);
  showWarnings(notices);

  state.activeStyle = state.catalog.default_style;
  const first = state.catalog.styles.find((s) => s.id === state.activeStyle);
  if (first) showStyleDetail(first);

  showHarmonySource(analysis);
  state.soloVoice = null;
  renderParts([]);

  $("stage-upload").hidden = true;
  $("stage-work").hidden = false;

  renderTimeline();
  renderPalette();
  updateSelectionUi();
  scheduleArrange(0);
}

function showWarnings(messages) {
  const box = $("warnings");
  if (!messages.length) { box.hidden = true; return; }
  box.innerHTML = "<ul>" + messages.map((m) => `<li>${escapeHtml(m)}</li>`).join("") + "</ul>";
  box.hidden = false;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ─────────────────────────────────────────────────────── timeline ──

/** Melody contour for one bar, drawn as note-length ticks at pitch height. */
function sparkline(bar, lo, hi) {
  const W = 78, H = 26, PAD = 3;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "tl-spark");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const notes = bar.melody.filter((n) => n[0] !== null);
  if (!notes.length) return svg;

  const barLength = notes.reduce((max, n) => Math.max(max, n[1] + n[2]), 0) || 4;
  const span = Math.max(1, hi - lo);
  for (const [pitch, offset, duration] of notes) {
    const x1 = PAD + (offset / barLength) * (W - PAD * 2);
    const x2 = PAD + ((offset + duration) / barLength) * (W - PAD * 2);
    const y = H - PAD - ((pitch - lo) / span) * (H - PAD * 2);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", x1.toFixed(1));
    line.setAttribute("x2", Math.max(x1 + 2, x2 - 1).toFixed(1));
    line.setAttribute("y1", y.toFixed(1));
    line.setAttribute("y2", y.toFixed(1));
    line.setAttribute("stroke-width", "2");
    line.setAttribute("stroke-linecap", "round");
    svg.append(line);
  }
  return svg;
}

function renderTimeline() {
  const timeline = $("timeline");
  timeline.innerHTML = "";

  const pitches = state.bars.flatMap((b) => b.melody.filter((n) => n[0] !== null).map((n) => n[0]));
  const lo = pitches.length ? Math.min(...pitches) : 60;
  const hi = pitches.length ? Math.max(...pitches) : 72;

  for (const bar of state.bars) {
    const cell = document.createElement("div");
    cell.className = "tl-bar";
    cell.dataset.index = String(bar.index);
    cell.style.setProperty("--swatch", colourFor(bar.style));
    cell.setAttribute("role", "option");

    const num = document.createElement("div");
    num.className = "tl-num";
    num.textContent = bar.index + 1;

    const chord = document.createElement("div");
    chord.className = "tl-chord" + (bar.chordOverride ? " overridden" : "");
    chord.textContent = bar.chordOverride || bar.chord;
    chord.title = bar.chordOverride ? `Overridden (detected: ${bar.chord})` : `Detected: ${bar.chord}`;

    const style = document.createElement("div");
    style.className = "tl-style";
    style.textContent = styleName(bar.style);

    cell.append(num, sparkline(bar, lo, hi), chord, style);
    timeline.append(cell);
  }
  syncTimelineSelection();
}

function syncTimelineSelection() {
  for (const cell of $("timeline").children) {
    const index = Number(cell.dataset.index);
    cell.classList.toggle("selected", state.selected.has(index));
    cell.classList.toggle("playing", index === state.currentBar);
    cell.setAttribute("aria-selected", String(state.selected.has(index)));
  }
}

/**
 * Refresh one bar's cell in place.
 *
 * Rebuilding the whole strip on every edit would be wasteful at 32+ bars and
 * would tear down the element a drag is currently tracking.
 */
function updateBarCell(index) {
  const cell = $("timeline").children[index];
  const bar = state.bars[index];
  if (!cell || !bar) return;

  cell.style.setProperty("--swatch", colourFor(bar.style));
  cell.querySelector(".tl-style").textContent = styleName(bar.style);

  const chord = cell.querySelector(".tl-chord");
  chord.textContent = bar.chordOverride || bar.chord;
  chord.classList.toggle("overridden", !!bar.chordOverride);
  chord.title = bar.chordOverride
    ? `Overridden (detected: ${bar.chord})`
    : `Detected: ${bar.chord}`;
}

// Drag across the timeline to select a range.
function installTimelineSelection() {
  const timeline = $("timeline");
  let dragging = false;
  let anchor = null;

  const barAt = (event) => {
    const cell = event.target.closest(".tl-bar");
    return cell ? Number(cell.dataset.index) : null;
  };

  timeline.addEventListener("mousedown", (event) => {
    const index = barAt(event);
    if (index === null) return;
    event.preventDefault();

    if (event.shiftKey && state.lastClickedBar !== null) {
      selectRange(state.lastClickedBar, index, event.ctrlKey || event.metaKey);
    } else if (event.ctrlKey || event.metaKey) {
      state.selected.has(index) ? state.selected.delete(index) : state.selected.add(index);
      state.lastClickedBar = index;
    } else {
      state.selected.clear();
      state.selected.add(index);
      state.lastClickedBar = index;
    }
    dragging = true;
    anchor = index;
    updateSelectionUi();
  });

  timeline.addEventListener("mouseover", (event) => {
    if (!dragging) return;
    const index = barAt(event);
    if (index === null || anchor === null) return;
    selectRange(anchor, index, false);
    updateSelectionUi();
  });

  window.addEventListener("mouseup", () => { dragging = false; anchor = null; });

  timeline.addEventListener("keydown", (event) => {
    if (event.key === "a" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      selectAll();
    } else if (event.key === "Escape") {
      state.selected.clear();
      updateSelectionUi();
    }
  });
}

function selectRange(from, to, additive) {
  if (!additive) state.selected.clear();
  const [lo, hi] = from <= to ? [from, to] : [to, from];
  for (let i = lo; i <= hi; i++) state.selected.add(i);
}

function selectAll() {
  state.bars.forEach((b) => state.selected.add(b.index));
  updateSelectionUi();
}

function updateSelectionUi() {
  syncTimelineSelection();

  const count = state.selected.size;
  const label = $("selection-label");
  const inspector = $("inspector");

  if (!count) {
    label.textContent = "No bars selected";
    inspector.hidden = true;
    return;
  }

  const indices = [...state.selected].sort((a, b) => a - b);
  label.textContent = count === 1
    ? `Bar ${indices[0] + 1} selected`
    : `${count} bars selected`;

  inspector.hidden = false;
  $("insp-title").textContent = count === 1
    ? `Bar ${indices[0] + 1}`
    : `Bars ${indices[0] + 1}–${indices[indices.length - 1] + 1}`;

  const styles = new Set(indices.map((i) => state.bars[i].style));
  const styleSelect = $("insp-style");
  styleSelect.value = styles.size === 1 ? [...styles][0] : "";
  if (styles.size > 1) styleSelect.selectedIndex = -1;

  const chordBox = $("insp-chord");
  if (count === 1) {
    const bar = state.bars[indices[0]];
    chordBox.disabled = false;
    chordBox.value = bar.chordOverride;
    chordBox.placeholder = bar.chord;
    $("insp-chord-hint").textContent = `Detected: ${bar.chord}`;
  } else {
    chordBox.disabled = true;
    chordBox.value = "";
    chordBox.placeholder = "select one bar";
    $("insp-chord-hint").textContent = "Chords are edited one bar at a time.";
  }
}

function applyStyleToBars(styleId, indices) {
  if (!indices.length) return;
  for (const index of indices) {
    if (!state.bars[index]) continue;
    state.bars[index].style = styleId;
    updateBarCell(index);
  }
  renderPalette();
  scheduleArrange();
  autosave();
}

// ──────────────────────────────────────────────────────── arrange ──

function scheduleArrange(delay = ARRANGE_DEBOUNCE_MS) {
  clearTimeout(state.arrangeTimer);
  $("sync-state").dataset.state = "working";
  state.arrangeTimer = setTimeout(runArrange, delay);
}

async function runArrange() {
  if (!state.session) return;

  // Supersede any request still in flight: only the newest result is wanted.
  state.arrangeAbort?.abort();
  const controller = new AbortController();
  state.arrangeAbort = controller;
  const seq = ++state.arrangeSeq;

  const payload = {
    session_id: state.session.session_id,
    ensemble: $("ensemble").value,
    default_style: state.catalog.default_style,
    transpose: Number($("transpose").value),
    tempo: Number($("tempo").value) || null,
    include_lyrics: $("include-lyrics").checked,
    lyrics: $("lyrics").value.trim() || null,
    lyrics_all_voices: $("lyrics-all-voices").checked,
    bars: state.bars.map((b) => ({
      index: b.index,
      style: b.style,
      chord: b.chordOverride || null,
    })),
  };

  try {
    const response = await fetch("/api/arrange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    const result = await response.json();
    if (seq !== state.arrangeSeq) return;  // a newer request already won

    const notices = [];
    if (state.session.transcription_note) notices.push(state.session.transcription_note);
    notices.push(...result.warnings);
    showWarnings(notices);

    state.arrangementId = result.arrangement_id;
    $("download-midi").href = `/api/arrangement/${result.arrangement_id}.mid`;
    $("download-xml").href = `/api/arrangement/${result.arrangement_id}.musicxml`;
    $("player").src = state.soloVoice === null
      ? `/api/arrangement/${result.arrangement_id}.mid`
      : `/api/arrangement/${result.arrangement_id}/practice/${state.soloVoice}.mid`;
    renderParts(result.voices || []);
    autosave();

    renderLyricStrip(result.lyric_layout || []);
    computeBarTimes();
    await renderScore(result.musicxml);
    $("sync-state").dataset.state = "ok";
  } catch (error) {
    if (error.name === "AbortError") return;
    $("sync-state").dataset.state = "error";
    setStatus($("score-status"), error.message, "error");
  }
}

/** Wall-clock span of each bar, for the playback cursor. */
function computeBarTimes() {
  const tempo = Number($("tempo").value) || state.session.tempo || 96;
  const secondsPerQuarter = 60 / tempo;
  let cursor = 0;
  state.barTimes = state.session.bars.map((bar) => {
    const quarters = bar.beats * (4 / bar.beat_type);
    const start = cursor;
    cursor += quarters * secondsPerQuarter;
    return { start, end: cursor };
  });
  state.duration = cursor;
}

// ────────────────────────────────────────────────────────── score ──

async function renderScore(musicxml) {
  const target = $("score");
  if (typeof opensheetmusicdisplay === "undefined") {
    setStatus($("score-status"),
      "Score preview needs the notation library, which could not be loaded. " +
      "Playback and downloads still work.", "error");
    return;
  }
  try {
    if (!state.osmd) {
      state.osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay(target, {
        autoResize: false,
        drawTitle: false,
        drawPartNames: true,
        backend: "svg",
      });
    }
    await state.osmd.load(musicxml);
    state.osmd.render();

    // The panel may have been laid out at zero width a moment ago.
    await new Promise(requestAnimationFrame);
    const svg = target.querySelector("svg");
    if (svg && svg.getBoundingClientRect().width < 1 && target.clientWidth > 0) {
      state.osmd.render();
    }
    measureScoreBars();
    setStatus($("score-status"), "");
  } catch (error) {
    setStatus($("score-status"),
      `Could not draw the score (${error.message}). Playback and downloads still work.`, "error");
  }
}

/**
 * Pixel rect of each bar in the rendered score.
 *
 * OSMD reports geometry in its own units; the SVG is drawn at ten pixels per
 * unit times the zoom. Each bar spans several staves, so the rect is the union
 * over them.
 */
function measureScoreBars() {
  state.measureRects = [];
  const list = state.osmd?.GraphicSheet?.MeasureList;
  if (!list) return;
  const scale = 10 * (state.osmd.zoom || 1);

  state.measureRects = list.map((staves) => {
    const live = staves.filter(Boolean);
    if (!live.length) return null;
    const boxes = live.map((m) => m.PositionAndShape);
    const x = Math.min(...boxes.map((b) => b.AbsolutePosition.x));
    const right = Math.max(...boxes.map((b) => b.AbsolutePosition.x + b.Size.width));
    const y = Math.min(...boxes.map((b) => b.AbsolutePosition.y));
    const bottom = Math.max(...boxes.map((b) => b.AbsolutePosition.y + b.Size.height));
    return { x: x * scale, y: y * scale, w: (right - x) * scale, h: (bottom - y) * scale };
  });
}

function highlightBarInScore(index, playing) {
  const rect = state.measureRects[index];
  const box = $("score-highlight");
  const score = $("score");
  if (!rect) { box.hidden = true; return; }

  // Rects are relative to the SVG, which sits inside a scrolling container.
  box.hidden = false;
  box.classList.toggle("playing", !!playing);
  box.style.left = `${rect.x + score.clientLeft + 6 - score.scrollLeft}px`;
  box.style.top = `${rect.y + 6}px`;
  box.style.width = `${rect.w}px`;
  box.style.height = `${rect.h}px`;
}

function installScoreClick() {
  $("score").addEventListener("click", (event) => {
    // Simple mode has no bar selection to reveal, so clicking the score is inert.
    if (state.mode !== "pro" || !state.measureRects.length) return;
    const score = $("score");
    const bounds = score.getBoundingClientRect();
    const x = event.clientX - bounds.left + score.scrollLeft - 6;
    const y = event.clientY - bounds.top - 6;
    const index = state.measureRects.findIndex(
      (r) => r && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h,
    );
    if (index < 0) return;
    state.selected.clear();
    state.selected.add(index);
    state.lastClickedBar = index;
    updateSelectionUi();
    highlightBarInScore(index, false);
    $("timeline").children[index]?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  });

  $("score").addEventListener("scroll", () => {
    if (!$("score-highlight").hidden) {
      highlightBarInScore(state.currentBar >= 0 ? state.currentBar : state.lastClickedBar, state.playing);
    }
  });
}

// ────────────────────────────────────────────────────── transport ──

function installTransport() {
  const player = $("player");

  $("play").addEventListener("click", () => {
    if (state.playing) player.stop();
    else player.start();
  });

  player.addEventListener("start", () => setPlaying(true));
  player.addEventListener("stop", () => { setPlaying(false); setCurrentBar(-1); });
  player.addEventListener("load", () => {
    state.duration = player.duration || state.duration;
    $("seek").max = String(state.duration || 100);
  });
  player.addEventListener("note", () => tickTransport());

  // html-midi-player only fires events per note, which is too coarse for a
  // smooth cursor, so drive the display from a frame loop while playing.
  const frame = () => {
    if (state.playing) tickTransport();
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  $("seek").addEventListener("input", (event) => {
    const seconds = Number(event.target.value);
    player.currentTime = seconds;
    $("time-display").textContent = formatTime(seconds);
    setCurrentBar(barAtTime(seconds));
  });

  document.addEventListener("keydown", (event) => {
    if (event.code !== "Space") return;
    const tag = event.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    event.preventDefault();
    state.playing ? player.stop() : player.start();
  });
}

function setPlaying(playing) {
  state.playing = playing;
  // SVGElement has no `hidden` IDL property, so setting `.hidden` on the icons
  // would only create a JS expando and never reach CSS. Toggle a class instead.
  $("play").classList.toggle("playing", playing);
  $("play").setAttribute("aria-label", playing ? "Stop" : "Play");
}

function tickTransport() {
  const player = $("player");
  const now = player.currentTime || 0;
  $("time-display").textContent = formatTime(now);
  $("seek").value = String(now);
  setCurrentBar(barAtTime(now));
}

function barAtTime(seconds) {
  return state.barTimes.findIndex((b) => seconds >= b.start && seconds < b.end);
}

function setCurrentBar(index) {
  if (index === state.currentBar) return;
  state.currentBar = index;
  syncTimelineSelection();
  if (index >= 0) {
    highlightBarInScore(index, true);
    $("timeline").children[index]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  } else if (state.selected.size !== 1) {
    $("score-highlight").hidden = true;
  }
}

async function previewStyle(styleId) {
  const player = $("preview-player");
  const main = $("player");
  if (state.playing) main.stop();
  player.src = `/api/preview/${styleId}.mid?ensemble=${encodeURIComponent($("ensemble").value)}`;
  const button = $("sd-preview");
  button.disabled = true;
  try {
    await new Promise((resolve) => {
      const done = () => { player.removeEventListener("load", done); resolve(); };
      player.addEventListener("load", done);
      setTimeout(done, 4000);
    });
    player.start();
    player.addEventListener("stop", () => { button.disabled = false; }, { once: true });
  } catch {
    button.disabled = false;
  }
}


// ─────────────────────────────────────────────────────────── modes ──

/**
 * Simple mode is the whole app minus the parts you only want when tuning:
 * pick who is singing and one style, hear it, download it. Pro adds per-bar
 * editing, chord overrides, transpose/tempo and the command box.
 */
function setMode(mode) {
  state.mode = mode === "pro" ? "pro" : "simple";
  document.body.dataset.mode = state.mode;
  $("mode-simple").setAttribute("aria-pressed", String(state.mode === "simple"));
  $("mode-pro").setAttribute("aria-pressed", String(state.mode === "pro"));
  try { localStorage.setItem(MODE_KEY, state.mode); } catch { /* private mode */ }

  $("palette-hint").textContent = state.mode === "simple"
    ? "Pick one style for the whole piece."
    : "Select bars below, then click a style to apply it.";

  if (state.mode === "simple") {
    // Leaving a selection behind would be invisible and would silently scope
    // the next edit, so drop it on the way in.
    state.selected.clear();
    updateSelectionUi();
  }
  if (state.catalog) renderPalette();

  // The score's width changes when panels appear or disappear.
  if (state.osmd && !$("stage-work").hidden) {
    requestAnimationFrame(() => {
      state.osmd.render();
      measureScoreBars();
      $("score-highlight").hidden = true;
    });
  }
}

// ────────────────────────────────────────────────────────── command ──

function snapshot() {
  return {
    bars: state.bars.map((b) => ({ style: b.style, chordOverride: b.chordOverride })),
    ensemble: $("ensemble").value,
    transpose: $("transpose").value,
    tempo: $("tempo").value,
  };
}

function restore(snap) {
  if (!snap) return;
  snap.bars.forEach((saved, index) => {
    if (!state.bars[index]) return;
    state.bars[index].style = saved.style;
    state.bars[index].chordOverride = saved.chordOverride;
    updateBarCell(index);
  });
  $("ensemble").value = snap.ensemble;
  $("transpose").value = snap.transpose;
  $("tempo").value = snap.tempo;
  renderPalette();
  updateSelectionUi();
  scheduleArrange();
}

function applyPlan(plan) {
  const touched = new Set();
  for (const action of plan.actions) {
    if (action.type === "style" && action.bars) {
      for (const index of action.bars) {
        if (!state.bars[index]) continue;
        state.bars[index].style = action.style;
        updateBarCell(index);
        touched.add(index);
      }
    } else if (action.type === "ensemble") {
      $("ensemble").value = action.value;
    } else if (action.type === "transpose") {
      $("transpose").value = String(action.value);
    } else if (action.type === "tempo") {
      $("tempo").value = String(action.value);
    }
  }
  renderPalette();
  scheduleArrange();
  return touched;
}

async function runCommand(text) {
  if (!text.trim() || !state.session) return;

  const before = snapshot();
  const result = $("command-result");
  result.hidden = false;
  result.className = "cmd-result";
  result.textContent = "Working…";

  let plan;
  try {
    plan = await api("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.session.session_id,
        text,
        tempo: Number($("tempo").value) || null,
      }),
    });
  } catch (error) {
    result.className = "cmd-result error";
    result.textContent = error.message;
    return;
  }

  if (!plan.understood) {
    result.className = "cmd-result error";
    result.textContent = plan.message || "I could not read that.";
    return;
  }

  state.undoSnapshot = before;
  applyPlan(plan);

  result.className = "cmd-result";
  result.innerHTML = "";
  const badge = document.createElement("span");
  badge.className = "badge" + (plan.source === "llm" ? " ai" : "");
  badge.textContent = plan.source === "llm" ? "AI" : "parsed";
  const text_ = document.createElement("span");
  text_.textContent = plan.summary;
  result.append(badge, text_);
  $("command-undo").hidden = false;
  $("command-input").value = "";
}

function installCommandBox() {
  $("command-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runCommand($("command-input").value);
  });

  $("command-undo").addEventListener("click", () => {
    restore(state.undoSnapshot);
    state.undoSnapshot = null;
    $("command-undo").hidden = true;
    $("command-result").hidden = true;
  });
}

function renderCommandExamples() {
  const host = $("command-examples");
  host.innerHTML = "";
  for (const example of state.catalog.command_examples || []) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "cmd-example";
    chip.textContent = example;
    chip.addEventListener("click", () => {
      $("command-input").value = example;
      runCommand(example);
    });
    host.append(chip);
  }
  $("command-engine").textContent = state.catalog.llm_enabled
    ? "Grammar first, AI for anything it cannot parse."
    : "Understood by a built-in grammar — no AI needed.";
}


// ────────────────────────────────────────────────────────────── pdf ──

let pdfLibsLoaded = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = src;
    tag.onload = resolve;
    tag.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.append(tag);
  });
}

function ensurePdfLibs() {
  if (!pdfLibsLoaded) {
    // Sequential: svg2pdf registers itself onto jsPDF, so order matters.
    pdfLibsLoaded = PDF_SCRIPTS.reduce(
      (chain, src) => chain.then(() => loadScript(src)), Promise.resolve(),
    ).catch((error) => { pdfLibsLoaded = null; throw error; });
  }
  return pdfLibsLoaded;
}

/**
 * Export the score as a real, multi-page vector PDF.
 *
 * No PDF engine is installed locally, so the score is re-engraved offscreen at
 * A4 — OSMD paginates it properly rather than producing one unreadably long
 * strip — and each page's SVG is drawn into the document as vectors, which
 * keeps it sharp and printable. The on-screen score is left untouched.
 */
async function exportPdf() {
  const button = $("download-pdf");
  if (button.disabled || !state.session) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Building…";

  let host = null;
  try {
    if (typeof opensheetmusicdisplay === "undefined") {
      throw new Error("the notation library could not be loaded");
    }
    await ensurePdfLibs();
    const musicxml = await (await fetch($("download-xml").href)).text();

    host = document.createElement("div");
    host.style.cssText = "position:absolute;left:-99999px;top:0;width:1000px;";
    document.body.append(host);

    const printer = new opensheetmusicdisplay.OpenSheetMusicDisplay(host, {
      autoResize: false, backend: "svg", drawTitle: true,
      drawPartNames: true, pageFormat: "A4_P",
    });
    await printer.load(musicxml);
    printer.render();

    const pages = [...host.querySelectorAll("svg")];
    if (!pages.length) throw new Error("nothing was engraved");

    const { jsPDF } = window.jspdf;
    const doc = new jsPDF({ orientation: "p", unit: "pt", format: "a4" });
    const pageWidth = doc.internal.pageSize.getWidth();
    const pageHeight = doc.internal.pageSize.getHeight();

    for (let i = 0; i < pages.length; i++) {
      if (i) doc.addPage();
      const box = pages[i].getBoundingClientRect();
      const scale = Math.min(pageWidth / box.width, pageHeight / box.height);
      await doc.svg(pages[i], {
        x: (pageWidth - box.width * scale) / 2,
        y: 0,
        width: box.width * scale,
        height: box.height * scale,
      });
    }

    const stem = (state.session.title || "arrangement").replace(/[^\w \-]/g, "_");
    doc.save(`${stem} (${$("ensemble").value}).pdf`);
    button.textContent = `${pages.length} page${pages.length === 1 ? "" : "s"} \u2713`;
    setTimeout(() => { button.textContent = original; }, 2200);
  } catch (error) {
    setStatus($("score-status"), `PDF export failed: ${error.message}`, "error");
    button.textContent = original;
  } finally {
    host?.remove();
    button.disabled = false;
  }
}


// ────────────────────────────────────────────────────────── lyrics ──

/**
 * Show which syllable landed on which note, grouped by bar.
 *
 * Built from the arrangement the server actually produced rather than by
 * re-running the syllable rules in JavaScript, so the strip can never disagree
 * with the engraved score. The cost is that it refreshes with the debounced
 * re-arrange rather than on every keystroke.
 */
function renderLyricStrip(layout) {
  const host = $("lyric-strip");
  const counter = $("lyric-count");

  if (!layout.length) {
    host.hidden = true;
    counter.textContent = "";
    return;
  }

  const sung = layout.filter(([, text]) => text).length;
  counter.textContent = `${sung} of ${layout.length} notes have a word`;
  counter.className = "muted" + (sung && sung < layout.length ? " short" : "");

  if (!sung) {
    host.hidden = true;
    return;
  }

  host.hidden = false;
  host.innerHTML = "";
  let currentBar = null;
  let cells = null;

  for (const [barIndex, text] of layout) {
    if (barIndex !== currentBar) {
      currentBar = barIndex;
      const group = document.createElement("div");
      group.className = "ls-bar";
      const label = document.createElement("div");
      label.className = "ls-bar-num";
      label.textContent = barIndex + 1;
      cells = document.createElement("div");
      cells.className = "ls-cells";
      group.append(label, cells);
      host.append(group);
    }
    const cell = document.createElement("span");
    cell.className = "ls-cell" + (text ? "" : " empty");
    cell.textContent = text || "\u00b7";
    cells.append(cell);
  }
}

async function hyphenateLyrics() {
  const box = $("lyrics");
  const text = box.value.trim();
  if (!text) return;
  const button = $("hyphenate");
  button.disabled = true;
  try {
    const result = await api("/api/hyphenate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, lang: $("lyric-lang").value }),
    });
    if (result.text !== box.value) {
      box.value = result.text;
      scheduleArrange(0);
    }
  } catch (error) {
    setStatus($("score-status"), error.message, "error");
  } finally {
    button.disabled = false;
  }
}


// ─────────────────────────────────────────────────────── harmony ──

function showHarmonySource(analysis) {
  const chip = $("harmony-source");
  const wrap = $("reinfer-wrap");
  const label = {
    symbols: "Chords: the file's own chord symbols",
    texture: "Chords: read from the file's parts",
    inferred: "Chords: inferred from the melody",
  }[analysis.harmony_source] || "";
  chip.textContent = label;
  chip.hidden = !label;
  // Only offer to re-infer when there was something better to begin with.
  wrap.hidden = analysis.harmony_source === "inferred";
}

// ──────────────────────────────────────────────────────────── fit ──

async function runFit() {
  const button = $("fit-button");
  const host = $("fit-results");
  if (!state.session) return;
  button.disabled = true;
  host.hidden = false;
  host.innerHTML = '<div class="fit-option">Trying every ensemble and key…</div>';
  try {
    const result = await api("/api/fit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session.session_id }),
    });
    host.innerHTML = "";
    if (!result.options.length) {
      host.innerHTML = '<div class="fit-option">No suggestions.</div>';
      return;
    }
    for (const [index, option] of result.options.slice(0, 5).entries()) {
      const row = document.createElement("div");
      row.className = "fit-option" + (index === 0 ? " best" : "");
      const text = document.createElement("span");
      text.className = "grow";
      text.textContent = option.summary;
      const metric = document.createElement("span");
      metric.className = "metric";
      metric.textContent = `${option.out_of_range} outside · strain ${option.strain}`;
      const apply = document.createElement("button");
      apply.type = "button";
      apply.className = "btn small";
      apply.textContent = "Use this";
      apply.addEventListener("click", () => {
        $("ensemble").value = option.ensemble;
        $("transpose").value = String(option.transpose);
        host.hidden = true;
        scheduleArrange(0);
      });
      row.append(text, metric, apply);
      host.append(row);
    }
  } catch (error) {
    host.innerHTML = "";
    setStatus($("score-status"), error.message, "error");
  } finally {
    button.disabled = false;
  }
}

// ────────────────────────────────────────────────────────── parts ──

function renderParts(voices) {
  state.voices = voices;
  const host = $("parts-list");
  host.innerHTML = "";

  for (const [index, name] of voices.entries()) {
    const row = document.createElement("div");
    row.className = "part-row" + (state.soloVoice === index ? " soloed" : "");

    const label = document.createElement("span");
    label.className = "part-name";
    label.textContent = name;

    const solo = document.createElement("button");
    solo.type = "button";
    solo.className = "btn small" + (state.soloVoice === index ? "" : " ghost");
    solo.textContent = state.soloVoice === index ? "Soloed" : "Solo";
    solo.addEventListener("click", () => {
      // Soloing swaps the player to that part's practice mix, so "solo" means
      // the same thing you hear in the downloaded track.
      state.soloVoice = state.soloVoice === index ? null : index;
      applySoloToPlayer();
      renderParts(voices);
    });

    const download = document.createElement("a");
    download.className = "btn small";
    download.textContent = "Practice track";
    download.download = "";
    download.href = state.arrangementId
      ? `/api/arrangement/${state.arrangementId}/practice/${index}.mid` : "#";

    row.append(label, solo, download);
    host.append(row);
  }

  if (voices.length) {
    const row = document.createElement("div");
    row.className = "part-row";
    const label = document.createElement("span");
    label.className = "part-name muted";
    label.textContent = "All parts, balanced";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn small" + (state.soloVoice === null ? "" : " ghost");
    button.textContent = "Play all";
    button.addEventListener("click", () => {
      state.soloVoice = null;
      applySoloToPlayer();
      renderParts(voices);
    });
    row.append(label, button);
    host.append(row);
  }
}

function applySoloToPlayer() {
  if (!state.arrangementId) return;
  const wasPlaying = state.playing;
  const at = $("player").currentTime || 0;
  $("player").src = state.soloVoice === null
    ? `/api/arrangement/${state.arrangementId}.mid`
    : `/api/arrangement/${state.arrangementId}/practice/${state.soloVoice}.mid`;
  if (wasPlaying) {
    $("player").addEventListener("load", () => {
      $("player").currentTime = at;
      $("player").start();
    }, { once: true });
  }
}

// ─────────────────────────────────────────────────────── projects ──

/**
 * Everything needed to rebuild this arrangement without the original file.
 *
 * Sessions live in the server's memory, so without this a page reload loses
 * the upload and every per-bar choice made since.
 */
function projectPayload() {
  if (!state.session) return null;
  return {
    v: PROJECT_VERSION,
    title: state.session.title,
    source_kind: state.session.source_kind,
    tempo: Number($("tempo").value) || state.session.tempo,
    key: state.session.key,
    bars: state.session.bars.map((bar) => ({
      beats: bar.beats,
      beat_type: bar.beat_type,
      length: bar.beats * (4 / bar.beat_type),
      melody: bar.melody,
      chord: bar.chord,
      roman: bar.roman,
    })),
    settings: {
      ensemble: $("ensemble").value,
      transpose: $("transpose").value,
      tempo: $("tempo").value,
      includeLyrics: $("include-lyrics").checked,
      lyrics: $("lyrics").value,
      lyricsAllVoices: $("lyrics-all-voices").checked,
      styles: state.bars.map((b) => b.style),
      chordOverrides: state.bars.map((b) => b.chordOverride),
    },
  };
}

function autosave() {
  clearTimeout(state.autosaveTimer);
  state.autosaveTimer = setTimeout(() => {
    const payload = projectPayload();
    if (!payload) return;
    try {
      localStorage.setItem(PROJECT_KEY, JSON.stringify({ saved: Date.now(), payload }));
    } catch {
      // Out of quota, or private mode. Clear our own entry so a later, smaller
      // save can still succeed, and leave the rest of the app working.
      try { localStorage.removeItem(PROJECT_KEY); } catch { /* nothing to do */ }
    }
  }, AUTOSAVE_MS);
}

function validateProject(payload) {
  if (!payload || typeof payload !== "object") throw new Error("not a project file");
  if (!Array.isArray(payload.bars) || !payload.bars.length) {
    throw new Error("it has no bars in it");
  }
  // A file written by a newer version may use fields this build does not
  // understand, so say so rather than silently loading half of it.
  const version = Number(payload.v ?? 0);
  if (version > PROJECT_VERSION) {
    throw new Error(`it was saved by a newer version (v${version}); this build reads v${PROJECT_VERSION}`);
  }
  return payload;
}

async function loadProject(rawPayload) {
  const payload = validateProject(rawPayload);
  const analysis = await api("/api/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: payload.title,
      source_kind: payload.source_kind,
      tempo: payload.tempo,
      bars: payload.bars,
    }),
  });
  applyAnalysis(analysis);

  const saved = payload.settings || {};
  if (saved.ensemble) $("ensemble").value = saved.ensemble;
  if (saved.transpose !== undefined) $("transpose").value = saved.transpose;
  if (saved.tempo) $("tempo").value = saved.tempo;
  if (saved.includeLyrics !== undefined) $("include-lyrics").checked = saved.includeLyrics;
  if (saved.lyrics !== undefined) $("lyrics").value = saved.lyrics;
  if (saved.lyricsAllVoices !== undefined) $("lyrics-all-voices").checked = saved.lyricsAllVoices;
  state.bars.forEach((bar, index) => {
    if (saved.styles?.[index]) bar.style = saved.styles[index];
    if (saved.chordOverrides?.[index]) bar.chordOverride = saved.chordOverrides[index];
    updateBarCell(index);
  });
  renderPalette();
  scheduleArrange(0);
}

function offerRestore() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(PROJECT_KEY) || "null"); } catch { stored = null; }
  try {
    validateProject(stored?.payload);
  } catch {
    // Corrupt, hand-edited or from a newer build: drop it rather than offering
    // to restore something that will fail halfway through.
    try { localStorage.removeItem(PROJECT_KEY); } catch { /* nothing to do */ }
    return;
  }

  const when = new Date(stored.saved);
  const banner = $("restore-banner");
  $("restore-text").textContent =
    `“${stored.payload.title}” from ${when.toLocaleString()} — ${stored.payload.bars.length} bars.`;
  banner.hidden = false;

  $("restore-yes").addEventListener("click", async () => {
    banner.hidden = true;
    setStatus($("upload-status"), "Restoring…");
    try {
      await loadProject(stored.payload);
      setStatus($("upload-status"), "");
    } catch (error) {
      setStatus($("upload-status"), error.message, "error");
    }
  }, { once: true });

  $("restore-no").addEventListener("click", () => {
    banner.hidden = true;
    try { localStorage.removeItem(PROJECT_KEY); } catch { /* nothing to do */ }
  }, { once: true });
}

function saveProjectFile() {
  const payload = projectPayload();
  if (!payload) return;
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${(payload.title || "arrangement").replace(/[^\w \-]/g, "_")}.acappella.json`;
  link.click();
  URL.revokeObjectURL(url);
}

// ───────────────────────────────────────────────────────── wiring ──





function installUpload() {
  const dropzone = $("dropzone");
  const input = $("file-input");

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files.length) chooseFile(input.files[0]);
    input.value = "";
  });

  for (const name of ["dragenter", "dragover"]) {
    dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
  }
  for (const name of ["dragleave", "drop"]) {
    dropzone.addEventListener(name, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); });
  }
  dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) chooseFile(event.dataTransfer.files[0]);
  });

  $("transcribe").addEventListener("click", () => {
    if (state.pendingAudio) uploadFile(state.pendingAudio);
  });
  $("cancel-audio").addEventListener("click", () => {
    state.pendingAudio = null;
    $("audio-options").hidden = true;
  });

  for (const button of document.querySelectorAll("[data-example]")) {
    button.addEventListener("click", async () => {
      const name = button.dataset.example;
      setStatus($("upload-status"), `Fetching “${name}”…`);
      try {
        const response = await fetch(`/samples/${name}`);
        if (!response.ok) throw new Error(`example not found (${response.status})`);
        chooseFile(new File([await response.blob()], name));
      } catch (error) {
        setStatus($("upload-status"), error.message, "error");
      }
    });
  }
}

function installControls() {
  $("back-to-upload").addEventListener("click", () => {
    if (state.playing) $("player").stop();
    $("stage-work").hidden = true;
    $("stage-upload").hidden = false;
  });

  for (const id of ["ensemble", "transpose", "tempo", "include-lyrics"]) {
    $(id).addEventListener("change", () => { scheduleArrange(); autosave(); });
  }

  $("download-pdf").addEventListener("click", exportPdf);
  $("fit-button").addEventListener("click", runFit);
  $("save-project").addEventListener("click", saveProjectFile);
  $("open-project").addEventListener("click", () => $("project-input").click());

  $("project-input").addEventListener("change", async () => {
    const file = $("project-input").files[0];
    $("project-input").value = "";
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      if (!payload?.bars?.length) throw new Error("that file has no bars in it");
      await loadProject(payload);
    } catch (error) {
      setStatus($("upload-status"), `Could not open that project: ${error.message}`, "error");
      $("stage-work").hidden = true;
      $("stage-upload").hidden = false;
    }
  });

  // Re-inferring changes the analysis, so it has to go back to the server.
  $("reinfer").addEventListener("change", async () => {
    if (!state.session) return;
    $("sync-state").dataset.state = "working";
    const form = new FormData();
    form.append("session_id", state.session.session_id);
    form.append("chords_per_bar", $("chords-per-bar").value);
    form.append("harmony_source", $("reinfer").checked ? "infer" : "auto");
    try {
      const analysis = await api("/api/reanalyze", { method: "POST", body: form });
      state.session = analysis;
      showHarmonySource(analysis);
      analysis.bars.forEach((bar, index) => {
        if (!state.bars[index]) return;
        state.bars[index].chord = bar.chord;
        state.bars[index].roman = bar.roman;
        updateBarCell(index);
      });
      updateSelectionUi();
      scheduleArrange(0);
    } catch (error) {
      setStatus($("score-status"), error.message, "error");
    }
  });
  $("hyphenate").addEventListener("click", hyphenateLyrics);

  let lyricsTimer = null;
  for (const id of ["lyrics", "lyrics-all-voices"]) {
    $(id).addEventListener("input", () => {
      clearTimeout(lyricsTimer);
      lyricsTimer = setTimeout(() => scheduleArrange(0), LYRICS_DEBOUNCE_MS);
    });
  }

  // Chords per bar re-runs the harmonic analysis, so it goes back to the
  // server rather than being applied client-side like the other settings.
  $("chords-per-bar").addEventListener("change", async () => {
    if (!state.session) return;
    $("sync-state").dataset.state = "working";
    const styles = state.bars.map((b) => b.style);
    const overrides = state.bars.map((b) => b.chordOverride);
    const form = new FormData();
    form.append("session_id", state.session.session_id);
    form.append("chords_per_bar", $("chords-per-bar").value);
    form.append("harmony_source", $("reinfer").checked ? "infer" : "auto");
    try {
      const analysis = await api("/api/reanalyze", { method: "POST", body: form });
      state.session = analysis;
      showHarmonySource(analysis);
      analysis.bars.forEach((bar, index) => {
        if (!state.bars[index]) return;
        state.bars[index].chord = bar.chord;
        state.bars[index].roman = bar.roman;
        // Keep the user's own choices across a re-analysis.
        state.bars[index].style = styles[index] ?? state.bars[index].style;
        state.bars[index].chordOverride = overrides[index] ?? "";
        updateBarCell(index);
      });
      updateSelectionUi();
      scheduleArrange(0);
    } catch (error) {
      $("sync-state").dataset.state = "error";
      setStatus($("score-status"), error.message, "error");
    }
  });

  $("mode-simple").addEventListener("click", () => setMode("simple"));
  $("mode-pro").addEventListener("click", () => setMode("pro"));

  $("select-all").addEventListener("click", selectAll);
  $("select-none").addEventListener("click", () => {
    state.selected.clear();
    updateSelectionUi();
  });

  $("insp-style").addEventListener("change", (event) => {
    applyStyleToBars(event.target.value, [...state.selected]);
    const style = state.catalog.styles.find((s) => s.id === event.target.value);
    if (style) { state.activeStyle = style.id; showStyleDetail(style); renderPalette(); }
  });

  const chordBox = $("insp-chord");
  chordBox.addEventListener("input", () => {
    if (state.selected.size !== 1) return;
    const index = [...state.selected][0];
    state.bars[index].chordOverride = chordBox.value.trim();
    updateBarCell(index);
    scheduleArrange();
  });

  $("insp-reset").addEventListener("click", () => {
    for (const index of state.selected) {
      state.bars[index].chordOverride = "";
      updateBarCell(index);
    }
    chordBox.value = "";
    scheduleArrange();
  });

  $("sd-preview").addEventListener("click", (event) => {
    const styleId = event.currentTarget.dataset.style;
    if (styleId) previewStyle(styleId);
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.osmd || $("stage-work").hidden) return;
      state.osmd.render();
      measureScoreBars();
      if (state.currentBar >= 0) highlightBarInScore(state.currentBar, state.playing);
      else $("score-highlight").hidden = true;
    }, 200);
  });
}

function init() {
  let saved = "simple";
  try { saved = localStorage.getItem(MODE_KEY) || "simple"; } catch { /* private mode */ }
  setMode(saved);

  installUpload();
  installControls();
  installCommandBox();
  installTimelineSelection();
  installScoreClick();
  installTransport();
  offerRestore();
  loadCatalog().catch((error) =>
    setStatus($("upload-status"), `Could not start up: ${error.message}`, "error"));
}

document.addEventListener("DOMContentLoaded", init);
