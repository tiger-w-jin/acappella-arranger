# A Cappella Arranger

Turns a melody into a singable a cappella arrangement. Feed it either a notated
score or an audio recording, pick a harmony style **for each bar**, and export
MusicXML and MIDI.

Everything runs locally. No audio, score or arrangement leaves the machine.

```
./setup.sh     # once
./run.sh       # then open http://127.0.0.1:8000
```

---

## What it does

**Two kinds of input.** Notated files (`.musicxml`, `.mxl`, `.xml`, `.mid`,
`.midi`, `.abc`, `.krn`) are parsed directly. Audio (`.wav`, `.mp3`, `.flac`,
`.m4a`, `.ogg`, …) is transcribed to a melody with Spotify's
[basic-pitch](https://github.com/spotify/basic-pitch), beat-tracked with
librosa, and quantized onto a bar grid. From there both paths are identical.

**Per-bar harmony styles.** Bar 1 can be a Bach chorale, bar 2 barbershop, and
bar 3 a gospel pad. Thirteen styles ship, differing in the colour tones they
add, how widely they voice, what rhythm the backing sings, and the syllable it
sings on:

| Style | Character |
|---|---|
| Chorale (SATB) | Classical four-part, moves with the melody, no parallel perfects |
| Open hymn pad | Wide sustained chords |
| Barbershop | Close harmony under the lead, barbershop sevenths and sixths |
| Jazz close harmony | 7ths and 9ths, tight, chromatic voice leading |
| Gospel 6/9 pad | Thick 6/9 and 13th voicings |
| Doo-wop | Triads on every beat |
| Rhythmic vamp | Eighth-note backing chords |
| Contemporary cluster | Added 9ths and 2nds, hummed |
| Suspended / airy | Sus2, no third |
| Open fifths (organum) | Bare parallel fifths and octaves |
| Pop thirds stack | Diatonic thirds and sixths below the tune |
| Pedal drone | Held root-and-fifth pedal |
| Unison / octaves | No harmony — clears space |

**Six ensembles.** SATB, SAB, SSAA, TTBB, SSATB, and a barbershop quartet whose
tenor sits *above* the lead. Each voice has a real range, and the arranger
tells you when a style asks for notes that part cannot sing.

**Output.** MusicXML (opens in MuseScore, Sibelius, Finale, Dorico), MIDI, plus
in-browser playback with a choir soundfont and an engraved score preview.

## Working in the app

Bars live in a single horizontal strip rather than a stack of dropdowns, so a
32-bar tune stays on one screen and its structure is visible at a glance: each
bar shows its melody contour, its chord, and a colour for its style.

- **Select** by clicking a bar, dragging across a range, shift-clicking to
  extend, or ctrl/cmd-clicking to add one. `Select all` does the obvious thing.
- **Apply** a style by clicking it in the palette — it lands on the selection
  immediately. The palette shows how many bars use each style.
- **Hear a style** before committing with *Hear it*, which plays the same
  three-bar ii-V-I rendered in that style, so the difference is audible rather
  than described.
- **Override a chord** for the selected bar by typing a symbol (`Fmaj7`, `Ab`,
  `G7sus4`, `C/G`). Overridden bars are marked with `*`.
- **Everything re-arranges automatically**, debounced, with in-flight requests
  cancelled so only the newest result is applied. There is no generate button.
- **The score and the strip are linked**: during playback the current bar is
  highlighted in both, and clicking any bar in the engraved score selects it.
- **Space** plays and stops.

---

## How the music is worked out

**Key** — Krumhansl-Schmuckler correlation over duration-weighted pitch
classes.

**Chords** — a Viterbi pass over the bars, not a bar-by-bar guess. Each
candidate chord is scored on how well it explains that bar's melody (weighted
by metric strength, so downbeats count for more), and each transition is scored
on how idiomatic the progression is (root motion, dominant resolution,
tonic/subdominant/dominant function). Choosing the whole sequence at once is
what stops a locally plausible chord from wrecking the cadence two bars later.
Bars of four beats or more may take two chords; shorter bars keep one.

Two details that took tuning. Melody notes score nearly the same whether
they're the root or the fifth of a chord — otherwise the analyser just chases
the melody and calls whatever note it lands on the root. And changing chords
mid-bar costs something, because real arrangements change harmony on barlines
far more often than inside them.

**Voicing** — an exhaustive search. For each chord the melody voice is fixed
and every legal pitch for every other voice is enumerated and scored on voice
leading, spacing, chord completeness, doubling, parallel fifths and octaves,
and how comfortable the note is in that part's range. The space is small (a few
hundred to a few thousand combinations), so brute force finds the true optimum
with no heuristics to go wrong. Voice crossing is a hard constraint rather than
a cost, applied in a first pass; only if the ranges make ordering impossible
does a second pass allow it.

You can override any bar's chord by typing a symbol (`Fmaj7`, `Ab`, `G7sus4`,
`C/G`) into that bar's chord box.

---

## Notes on audio input

Transcription is approximate and the app tells you what it did. Two artefacts
dominate, and both are corrected:

- **Fragmentation.** On a sustained note the model re-triggers mid-note and
  splits it into a run of short repeats. Contiguous same-pitch detections are
  merged back together. If your melody genuinely repeats notes, turn off *Merge
  repeated notes* under Input options.
- **Pitch wobble.** Vibrato and note releases wander a semitone or two, showing
  up as extra notes. These are dropped — but only when they are *quiet*
  relative to the note they hang off. Loudness, not brevity, is the reliable
  signal: the model often clips a real note short, and filtering on length
  alone eats genuine notes.

Time signature is not detected from audio; it defaults to 4/4 and you can
change it before uploading. Always check the melody and chords before
arranging.

---

## Layout

```
app/
  main.py            FastAPI endpoints
  models.py          internal score representation + API schemas
  theory.py          pitch, chord, key primitives (pure ints, no music21)
  analysis.py        key detection, chord inference, chord-symbol parsing
  preview.py         the ii-V-I demo the style palette auditions
  ingest/
    score.py         MusicXML / MIDI / ABC parsing, bar layout
    audio.py         basic-pitch transcription and line cleanup
  harmony/
    styles.py        ensembles, voice ranges, the style catalogue
    voicing.py       the voicing search and the non-search generators
    arranger.py      per-bar styles -> a complete multi-part arrangement
  export.py          music21 score -> MusicXML / MIDI
  static/            the web UI (no build step, vanilla JS)
tests/               246 tests: pipeline, every style x ensemble, audio
samples/             a notated melody and a recording, used by the UI examples
```

`theory.py` deliberately holds no music21 objects — they are far too slow to
put inside the voicing search's inner loop.

## Tests

```
.venv/bin/python -m pytest tests -q
```

Covers every style against every ensemble (rendering, no voice crossing, no
out-of-range writing), key and chord analysis, chord-symbol round trips,
transposition, MusicXML/MIDI export, style previews (including that no two
styles render identically), and real transcription of a synthesised melody.

## Known limits

- Scanned sheet music (PDF or images) is not supported; optical music
  recognition needs a separate engine. MusicXML from MuseScore works well.
- PDF export would need MuseScore or LilyPond installed; MusicXML covers it.
- Audio transcription follows one melodic line. Dense mixes transcribe poorly —
  a solo voice or lead instrument works best.
- Time signature and pickup detection come from notated input only.
