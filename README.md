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

**It reads the harmony a file already has.** A lead sheet states its chords and
a hymn or chorale states them in four parts, so guessing them back from the
melody would be throwing away the answer and getting a different one. Chord
symbols are believed outright; a multi-part score has its chords read off the
real texture, weighted by how long each note sounds and by the bass, which is
the strongest single clue to a root. Only a single line — a melody, or anything
from audio — falls back to inference, where it is the only option. The UI says
which of the three was used, and you can force a re-inference to reharmonise a
hymn on purpose.

Measured against music21's own analysis of five Bach chorales, reading the
parts lifts root accuracy from **51% to 57%** over 184 chord spans; on BWV 324
it is 50% to 83%. It is not uniformly better — BWV 281 goes the other way — but
it wins on average and it is exact whenever chord symbols are present.

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

**Fit to my singers** goes further than the warning: it tries every ensemble
against every key and ranks them by how singable the result is, counting notes
outside a part's range and how hard the rest push toward the edges. A
materially easier key wins, but a negligible gain will not reprint everyone's
music — staying put breaks the tie.

**Practice tracks.** Solo any part to hear it brought forward against the
others, or download it as a MIDI to learn from. The other voices stay audible
rather than muted, because you need to hear how your line sits.

**Lyrics.** Type the words in plain prose and press **Auto-hyphenate**:

```
Amazing grace how sweet the sound   ->   A-ma-zing grace how sweet the sound
```

The result lands in an editable box, because hyphenation is a first draft and
not an oracle. You can also write the hyphens yourself — a space starts a word,
a hyphen splits one across notes, a lone underscore holds a syllable over one
more note — and a manual hyphen is never second-guessed.

An **alignment strip** under the box shows which syllable landed on which note,
grouped by bar, so a miscount is obvious without reading the engraved score. It
is built from the arrangement the server actually produced rather than from a
second copy of the rules in JavaScript, so it cannot disagree with the score.

Lyrics already present in an uploaded MusicXML are loaded into the box with
their hyphens intact, ready to edit rather than retype.

By default only the melody carries the words and the backing sings its style
syllable; one checkbox puts them under every part, which is what you want for
homophonic writing. Words reach a backing part only where its chord lands on a
melody note — a held pad cannot sing them — and any mismatch between syllables
and notes is reported rather than hidden.

*On the hyphenator:* English uses rules written for singing, not for
typesetting. Hyphenation dictionaries exist to break words at line ends and
deliberately leave `twinkle`, `little`, `above` and `mercy` whole, which is
exactly wrong for a singer. `pyphen` covers the other languages in the dropdown,
where it is far better than anything short enough to write by hand; it is
optional, and English works without it.

**Your work survives a reload.** The arrangement autosaves locally and offers
to restore itself, and **Save** / **Open** write it out as a small `.json` you
can keep or share. Sessions live in the server's memory, so without this a
refresh meant re-uploading the file and redoing every choice.

**Output.** MusicXML (opens in MuseScore, Sibelius, Finale, Dorico), MIDI, PDF,
plus in-browser playback with a choir soundfont and an engraved score preview.
All three carry the lyrics. The MIDI lyric track holds the melody words alone —
backing syllables belong in the score but would mislead a karaoke player or a
DAW lyric view.

PDF is produced in the browser: no PDF engine is installed locally, so the
score is re-engraved offscreen at A4 — OSMD paginates it properly instead of
producing one unreadably long strip — and each page is written as vectors, so
it stays sharp at any zoom and prints cleanly. The libraries for it load only
the first time you export one.

## Two modes

**Simple** is the default: choose who is singing and one style for the whole
piece, listen, download. Nothing else is on screen.

**Pro** adds everything below — per-bar styles, chord overrides, transpose,
tempo, chords-per-bar, backing syllables, lyrics, and the command box. The
choice is remembered.

## Working in Pro mode

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

## Typing commands

Pro mode has a command box that takes plain instructions:

```
bars 9-16 barbershop, the rest chorale
first 8 chorale, the rest gospel
everything except the last 4 doo-wop
sing it as a barbershop quartet
transpose up a tone and set tempo 120
```

It understands bar ranges (`bars 9-16`, `bars 9 to 16`, `bar 5`,
`bars 1, 3 and 5`), `first`/`last N`, `all`, `the rest`, `except`, every style
under several names (`bach`, `doowop`, `organum`, `crunchy`, …), ensembles,
transpose (`up a tone`, `down a fifth`) and tempo (`120 bpm`, `faster`).
Clauses combine with commas and semicolons. Whatever it does is echoed back in
words and can be undone in one click.

**This is a grammar, not a model.** For a language this shaped, a parser is the
better engine: it is exact, runs in under a millisecond, needs no network or
credentials, and — unlike a model — its behaviour is pinned down by tests. It
is also honest about failure, saying it did not understand instead of guessing.

**The LLM is an optional fallback**, used only for phrasing the grammar
rejects, and only when `GEMINI_API_KEY` is set:

```
GEMINI_API_KEY=... ./run.sh      # optional; everything works without it
```

That path is for genuinely fuzzy requests ("build towards the end"). Its
output is validated back through the same action types, so an invented style
id or an out-of-range bar is discarded rather than applied, and any failure
degrades to "I could not read that" rather than an error.

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
  commands.py        the typed-command grammar
  llm.py             optional Gemini fallback, off unless GEMINI_API_KEY is set
  lyrics.py          syllable parsing, English syllabification, fitting words to notes
  fit.py             ranking ensembles and keys by how singable they are
  preview.py         the ii-V-I demo the style palette auditions
  export.py          music21 score -> MusicXML / MIDI / practice tracks
  ingest/
    score.py         MusicXML / MIDI / ABC parsing, bar layout
    audio.py         basic-pitch transcription and line cleanup
    harmony_source.py  chord symbols and multi-part texture read from the file
  harmony/
    styles.py        ensembles, voice ranges, the style catalogue
    voicing.py       the voicing search and the non-search generators
    arranger.py      per-bar styles -> a complete multi-part arrangement
  static/            the web UI (no build step, vanilla JS)
tests/               390 tests: pipeline, styles x ensembles, audio, commands, lyrics, sources
samples/             a melody, a 32-bar tune, a four-part chorale and a recording
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
styles render identically), real transcription of a synthesised melody, the
command grammar, lyric parsing, English syllabification, the alignment layout,
the MIDI lyric track's contents, and the validation that stops a bad LLM
response reaching the arranger.

## Known limits

- Scanned sheet music (PDF or images) is not supported as *input*; optical
  music recognition needs a separate engine. MusicXML from MuseScore works well.
- PDF *export* needs a network connection the first time you use it, since the
  conversion libraries come from a CDN. Everything else runs offline.
- Audio transcription follows one melodic line. Dense mixes transcribe poorly —
  a solo voice or lead instrument works best.
- Time signature and pickup detection come from notated input only.
