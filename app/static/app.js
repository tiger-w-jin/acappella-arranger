"use strict";

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

const state = {
  catalog: null,
  session: null,
  bars: [],          // { index, chord, roman, melody, style, chordOverride }
  selected: new Set(),
  osmd: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);

function noteName(midi) {
  if (midi === null || midi === undefined) return "–";
  return NOTE_NAMES[((midi % 12) + 12) % 12] + (Math.floor(midi / 12) - 1);
}

function setStatus(element, message, kind = "") {
  element.textContent = message;
  element.className = "status" + (kind ? " " + kind : "") +
    (element.id === "arrange-status" ? " inline-status" : "");
  element.hidden = !message;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* response had no JSON body */ }
    throw new Error(detail);
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

async function loadCatalog() {
  state.catalog = await api("/api/catalog");

  const ensemble = $("ensemble");
  ensemble.innerHTML = "";
  for (const item of state.catalog.ensembles) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.name} — melody in ${item.melody_voice}`;
    ensemble.append(option);
  }
  ensemble.value = "satb";

  for (const select of [$("default-style"), $("bulk-style")]) {
    select.innerHTML = "";
    for (const style of state.catalog.styles) {
      const option = document.createElement("option");
      option.value = style.id;
      option.textContent = style.name;
      option.title = style.description;
      select.append(option);
    }
    select.value = state.catalog.default_style;
  }

  const transpose = $("transpose");
  transpose.innerHTML = "";
  for (let semitones = -12; semitones <= 12; semitones++) {
    const option = document.createElement("option");
    option.value = String(semitones);
    option.textContent = semitones === 0
      ? "Original key"
      : `${semitones > 0 ? "+" : ""}${semitones} semitone${Math.abs(semitones) === 1 ? "" : "s"}`;
    transpose.append(option);
  }
  transpose.value = "0";

  $("accepted-types").textContent =
    `Scores: ${state.catalog.accepted_score.join(", ")} · Audio: ${state.catalog.accepted_audio.join(", ")}`;
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------

async function uploadFile(file) {
  if (!file || state.busy) return;
  state.busy = true;
  $("dropzone").classList.add("busy");

  const isAudio = /\.(wav|mp3|flac|ogg|m4a|aac|aiff|aif|wma)$/i.test(file.name);
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

  try {
    const analysis = await api("/api/upload", { method: "POST", body: form });
    applyAnalysis(analysis);
    setStatus($("upload-status"), `Loaded “${analysis.title}”.`, "ok");
  } catch (error) {
    setStatus($("upload-status"), error.message, "error");
  } finally {
    state.busy = false;
    $("dropzone").classList.remove("busy");
  }
}

function applyAnalysis(analysis) {
  state.session = analysis;
  state.selected.clear();
  state.bars = analysis.bars.map((bar) => ({
    index: bar.index,
    chord: bar.chord,
    roman: bar.roman,
    melody: bar.melody,
    style: $("default-style").value,
    chordOverride: "",
  }));

  $("summary").innerHTML = "";
  const chips = [
    ["Title", analysis.title],
    ["Source", analysis.source_kind === "audio" ? "audio transcription" : "notated score"],
    ["Key", `${analysis.key} (confidence ${analysis.key_confidence})`],
    ["Time", analysis.time_signature],
    ["Tempo", `${Math.round(analysis.tempo)} BPM`],
    ["Bars", analysis.bar_count],
  ];
  for (const [label, value] of chips) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `${label} <b></b>`;
    chip.querySelector("b").textContent = value;
    $("summary").append(chip);
  }

  const note = $("transcription-note");
  note.textContent = analysis.transcription_note || "";
  note.hidden = !analysis.transcription_note;

  $("tempo").value = Math.round(analysis.tempo);
  renderBars();

  $("arrange-panel").hidden = false;
  $("result-panel").hidden = true;
  $("arrange-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------------------
// Bar grid
// ---------------------------------------------------------------------------

function renderBars() {
  const container = $("bars");
  container.innerHTML = "";

  for (const bar of state.bars) {
    const card = document.createElement("div");
    card.className = "bar" + (state.selected.has(bar.index) ? " selected" : "");
    card.dataset.index = String(bar.index);

    const pitches = bar.melody
      .filter((entry) => entry[0] !== null)
      .map((entry) => noteName(entry[0]));

    const head = document.createElement("div");
    head.className = "bar-head";
    const number = document.createElement("span");
    number.className = "bar-number";
    number.textContent = `Bar ${bar.index + 1}`;
    const roman = document.createElement("span");
    roman.className = "bar-roman";
    roman.textContent = bar.roman || "";
    head.append(number, roman);

    const melody = document.createElement("div");
    melody.className = "bar-melody";
    melody.textContent = pitches.length ? pitches.join(" ") : "(rest)";
    melody.title = melody.textContent;

    const styleField = document.createElement("div");
    styleField.className = "field";
    const styleLabel = document.createElement("span");
    styleLabel.className = "field-label";
    styleLabel.textContent = "Harmony style";
    const styleSelect = document.createElement("select");
    for (const style of state.catalog.styles) {
      const option = document.createElement("option");
      option.value = style.id;
      option.textContent = style.name;
      option.title = style.description;
      styleSelect.append(option);
    }
    styleSelect.value = bar.style;
    styleSelect.addEventListener("change", (event) => {
      event.stopPropagation();
      bar.style = styleSelect.value;
    });
    styleSelect.addEventListener("click", (event) => event.stopPropagation());
    styleField.append(styleLabel, styleSelect);

    const chordField = document.createElement("div");
    chordField.className = "field";
    const chordLabel = document.createElement("span");
    chordLabel.className = "field-label";
    chordLabel.textContent = "Chord";
    const chordInput = document.createElement("input");
    chordInput.type = "text";
    chordInput.placeholder = bar.chord;
    chordInput.value = bar.chordOverride;
    chordInput.title = `Detected: ${bar.chord}. Type to override, e.g. Fmaj7, Ab, G7sus4.`;
    chordInput.addEventListener("input", () => {
      bar.chordOverride = chordInput.value.trim();
      chordInput.classList.remove("invalid");
    });
    chordInput.addEventListener("click", (event) => event.stopPropagation());
    chordField.append(chordLabel, chordInput);

    card.append(head, melody, styleField, chordField);
    card.addEventListener("click", () => {
      if (state.selected.has(bar.index)) state.selected.delete(bar.index);
      else state.selected.add(bar.index);
      card.classList.toggle("selected");
    });

    container.append(card);
  }
}

function applyStyle(indices, styleId) {
  for (const bar of state.bars) {
    if (indices === null || indices.has(bar.index)) bar.style = styleId;
  }
  renderBars();
}

// ---------------------------------------------------------------------------
// Arrange
// ---------------------------------------------------------------------------

async function createArrangement() {
  if (!state.session || state.busy) return;
  state.busy = true;
  $("arrange").disabled = true;
  setStatus($("arrange-status"), "Working out the voicings…");

  const payload = {
    session_id: state.session.session_id,
    ensemble: $("ensemble").value,
    default_style: $("default-style").value,
    transpose: Number($("transpose").value),
    tempo: Number($("tempo").value) || null,
    include_lyrics: $("include-lyrics").checked,
    bars: state.bars.map((bar) => ({
      index: bar.index,
      style: bar.style,
      chord: bar.chordOverride || null,
    })),
  };

  try {
    const result = await api("/api/arrange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await showResult(result);
    setStatus($("arrange-status"), "Done.", "ok");
  } catch (error) {
    setStatus($("arrange-status"), error.message, "error");
  } finally {
    state.busy = false;
    $("arrange").disabled = false;
  }
}

async function showResult(result) {
  $("result-panel").hidden = false;

  const warnings = $("warnings");
  if (result.warnings.length) {
    warnings.innerHTML = "<ul>" +
      result.warnings.map((text) => `<li>${escapeHtml(text)}</li>`).join("") + "</ul>";
    warnings.hidden = false;
  } else {
    warnings.hidden = true;
  }

  const midiUrl = `/api/arrangement/${result.arrangement_id}.mid`;
  $("download-midi").href = midiUrl;
  $("download-xml").href = `/api/arrangement/${result.arrangement_id}.musicxml`;

  // Point both at the MIDI. The player's `visualizer` attribute alone does not
  // reliably populate the piano roll, so the visualizer loads the file too.
  $("player").src = midiUrl;
  $("viz").src = midiUrl;

  $("result-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  await renderScore(result.musicxml);
}

async function renderScore(musicxml) {
  const target = $("score");
  if (typeof opensheetmusicdisplay === "undefined") {
    setStatus($("score-status"),
      "Score preview needs the notation library, which could not be loaded. " +
      "Playback and downloads still work.", "error");
    return;
  }
  setStatus($("score-status"), "Engraving…");
  try {
    if (!state.osmd) {
      state.osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay(target, {
        autoResize: true,
        drawTitle: true,
        drawPartNames: true,
        backend: "svg",
      });
    }
    await state.osmd.load(musicxml);
    state.osmd.render();

    // The panel has only just been unhidden, so the container may still have
    // measured as zero-width. Re-engrave once layout has settled if so.
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const svg = target.querySelector("svg");
    if (svg && svg.getBoundingClientRect().width < 1 && target.clientWidth > 0) {
      state.osmd.render();
    }
    setStatus($("score-status"), "");
  } catch (error) {
    setStatus($("score-status"),
      `Could not draw the score (${error.message}). Playback and downloads still work.`, "error");
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function init() {
  const dropzone = $("dropzone");
  const input = $("file-input");

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => {
    if (input.files.length) uploadFile(input.files[0]);
    input.value = "";
  });

  for (const name of ["dragenter", "dragover"]) {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragover");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    });
  }
  dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) uploadFile(event.dataTransfer.files[0]);
  });

  for (const button of document.querySelectorAll("[data-example]")) {
    button.addEventListener("click", async () => {
      const name = button.dataset.example;
      setStatus($("upload-status"), `Fetching the example “${name}”…`);
      try {
        const response = await fetch(`/samples/${name}`);
        if (!response.ok) throw new Error(`example not found (${response.status})`);
        const blob = await response.blob();
        await uploadFile(new File([blob], name));
      } catch (error) {
        setStatus($("upload-status"), error.message, "error");
      }
    });
  }

  $("apply-all").addEventListener("click", () => applyStyle(null, $("bulk-style").value));
  $("apply-selected").addEventListener("click", () => {
    if (!state.selected.size) {
      setStatus($("arrange-status"), "Select one or more bars first.", "error");
      return;
    }
    applyStyle(state.selected, $("bulk-style").value);
  });
  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    renderBars();
  });

  $("default-style").addEventListener("change", () => {
    $("bulk-style").value = $("default-style").value;
  });

  $("chords-per-bar").addEventListener("change", async () => {
    if (!state.session) return;
    const form = new FormData();
    form.append("session_id", state.session.session_id);
    form.append("chords_per_bar", $("chords-per-bar").value);
    try {
      const styles = state.bars.map((bar) => bar.style);
      const overrides = state.bars.map((bar) => bar.chordOverride);
      const analysis = await api("/api/reanalyze", { method: "POST", body: form });
      applyAnalysis(analysis);
      // Keep whatever the user had already chosen per bar.
      state.bars.forEach((bar, index) => {
        if (styles[index]) bar.style = styles[index];
        if (overrides[index]) bar.chordOverride = overrides[index];
      });
      renderBars();
    } catch (error) {
      setStatus($("upload-status"), error.message, "error");
    }
  });

  $("arrange").addEventListener("click", createArrangement);

  loadCatalog().catch((error) =>
    setStatus($("upload-status"), `Could not start up: ${error.message}`, "error"));
}

document.addEventListener("DOMContentLoaded", init);
