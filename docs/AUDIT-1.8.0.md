# Project Audit — 1.8.0

Audit date: 2026-08-02
Audited version: `1.8.0` (`master` @ `55d6e70`, clean working tree)

This audit follows `docs/AUDIT-1.7.0.md` and covers the work done after it:
F-27 (`7deac1b`), F-16 (`8c9eaae`), F-18 (`d729ee5`), F-20 (`840e5dd`), and
F-24 (`55d6e70`). Unlike the 1.7.0 audit, most findings here are **not**
code-reading hypotheses: they were measured against the user's real
276-video / 97.5 GB test library at `S:\Daedalus\Kiroku\TestLib`
(local, not in the repository).

Severity uses the 1.7.0 scale: **Critical** = the feature does not work at
real scale; **High** = user-visible malfunction or serious slowdown;
**Medium** = correctness gap with a narrow trigger; **Low** = hygiene.

## Method

Read in full: the diff of every file touched since `30163b9`, plus
`video_fingerprint`, `image_fingerprint`, `audio_fingerprint`,
`folder_compare`, `service`, `report`, `benchmark`, `thresholds`, `cli`, and
`gui/main_window`.

Baseline checks on the project `.venv` (Python 3.11.9):

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **125 passed, 7 subtests** |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 62 files already formatted |
| `mypy dedupe` | 37 errors — unchanged documented baseline |
| End-to-end CLI scan (synthetic) | HTML/JSON written, sections/counts/filter correct |

So the *stated* verification of the recent work holds. What it did not cover
is a full-library run — the 1.7.0 audit records that the post-F-24 sanity
scan of this same 298-file library was interrupted by a machine restart and
never re-run. That gap is where every Critical finding below lives.

## Summary of outcome

The F-24 calibration work is sound in itself: on a set of six visually
distinct generated videos the new default threshold produces **zero** false
positives, and the rescale does what it claims for video and audio.

The problems are:

1. **The scanner cannot finish a real library.** F-25's lowered alignment
   floor and F-27's five offsets combine so that the expensive alignment
   recheck fires on essentially every duration-compatible pair, at a
   measured 10.3–12.3 s per pair, none of it cached between scans. On the
   276-file test library that is **~39 hours per scan** (A-01).
2. **The shipped threshold was validated on pairs, not on a library.** At
   library scale the 15.0 default produces real false positives that the
   76-pair corpus could not reveal (A-02).
3. **Two migration gaps** silently disable matching for anyone who used the
   1.7.8/1.7.9 GUI, or who runs with the local working config (A-03, A-04).

Everything else is smaller.

---

## Critical

### A-01 · The alignment recheck makes a full scan take ~39 hours

`ALIGNMENT_RECHECK_FLOOR` was lowered from `65.0` to `40.0` (F-25) at the
same time as `ALIGNMENT_OFFSET_STEPS` rose from 2 to 5 (F-27). The floor was
intended as a cost circuit-breaker. It no longer breaks anything:

```
raw similarity of unrelated 15-frame fingerprints (200 random pairs):
  min 49.56   p05 50.56   mean 52.24   max 55.52
  fraction >= ALIGNMENT_RECHECK_FLOOR (40): 1.000
```

Every pair clears it, by construction — the floor sits *below* the Hamming
noise floor it was meant to filter against. So the recheck's real gate is
only `0.05 < delta <= 180s`, which on a library of similar-length episodes
is most pairs.

Measured on `TestLib` (276 videos, 97.5 GB):

| Quantity | Value |
| --- | ---: |
| All pairs | 37 950 |
| Duration-compatible pairs | 22 849 |
| Recheck-eligible pairs (`0.05 < delta <= 180s`) | **13 541** (59%) |
| Aligned fingerprints implied (5 offsets) | 67 705 |
| ffmpeg frame extractions implied | 1 015 575 |
| Measured cost, random 40-pair sample | **10.34 s/pair** |
| Measured cost, 40 near-threshold pairs | 11.3–12.3 s/pair |
| **Extrapolated recheck time per scan** | **~39 hours** |

For comparison, the same scan with the recheck disabled completes in
**313 seconds** from a cold cache, and the pair-comparison arithmetic itself
costs **0.40 ms/pair** (5.4 s for all 13 541). All of the cost is ffmpeg
re-decoding.

Three properties make it worse than the raw number suggests:

- **Nothing is cached.** `aligned_fingerprints` is a per-scan in-memory dict
  keyed `(longer, shorter.duration, offset)`; because the key includes the
  *other* file's duration it almost never repeats, and it is discarded at
  the end of the scan. Every scan repays in full.
- **It is single-threaded.** The pair loop does not use the worker pool.
- **Most of it is provably wasted.** On a random 40-pair sample the recheck
  promoted **0** pairs; on 25 pairs sampled from the 10–15 band (the ones it
  exists to rescue) it promoted **0**, and 20 of the 25 gained exactly
  `+0.00`.

This is what the user observed as "some test runs took 80+ minutes and never
finished properly": 80 minutes is roughly 3% of the work.

**The recheck must not simply be removed** — see A-02: it is the single
strongest discriminator the matcher has. It needs to stop re-decoding.

**Fix (implemented in 1.9.0):** sample each file once on an *absolute* time
grid, cache that sequence, and align by sliding integer offsets over cached
hashes. Proof of concept measured on real `TestLib` pairs before
implementing:

| Pair | Ground truth | Current | Absolute grid |
| --- | --- | ---: | ---: |
| `Mesudachi-The-Animation-2` ↔ `Мои горячие подружки 2` | duplicate | 15.34 | **91.33** |
| `Shigokare` (two releases) | duplicate | 15.19 | **26.94** |
| `Baku Ane Otouto Shibocchau` (two releases) | duplicate | 16.04 | **20.86** |
| `Kimekoi Ep.2` ↔ `Shishunki-Sex-1` | unrelated | 15.47 | **9.38** |
| `Girls-Rush-1` ↔ `Girls-Rush-2` | different episodes | 15.64 | **7.81** |

```
one-time grid fingerprinting: 7.8 s/file (cached; 276 files ≈ 9 min on 4 workers)
per-pair alignment:           2.2 ms, no ffmpeg   (was 11.3 s)
```

The separation is *better* than today's, not merely cheaper. Note this does
not contradict the 1.7.0 audit's withdrawn "one ffmpeg pass with a `select`
filter" idea (that measurement stands — fast seeks beat a full decode); the
grid keeps fast seeks and simply stops repeating them per pair.

One honest caveat from the proof of concept: `Bikyaku Seido Kaichou`
`01_dub` ↔ `01_rus` (a true duplicate) scored 16.53 today and 12.50 on the
prototype, which scored a plain mean Hamming without the coverage-penalty DP
the real matcher uses. The shipped implementation therefore aligns on the
grid but scores with the existing `ordered_sequence_similarity`.

### A-02 · The video threshold was validated on 76 curated pairs, not on a library

`docs/THRESHOLDS.md` and `docs/STATUS.md` state "0 false positives" for
`video_threshold=15.0`. That is true of the 76-pair corpus and does not
transfer: a real scan compares **22 849** pairs, so the base rate of
unrelated pairs is roughly three orders of magnitude higher.

On the full `TestLib` scan, 29 of 82 reported matches sit in the 15–19 band,
and that band mixes both classes:

```
true duplicates                          false positives
21.50 Vampire_01_sub ↔ Vampire_01_sub    15.44 Venus Blood Brave/Ep.2 ↔ Boku Dake/Ep.1
22.89 Genkaku Cool na Sensei 01 ↔ _sub   15.47 Kimekoi Ep.2 ↔ Shishunki-Sex-1
16.53 Bikyaku Seido Kaichou dub ↔ rus    17.33 Aku no Onna Kanbu Ep.1 ↔ Ep.2
17.33 Yamitsuki Pheromone Ep.2 ↔ _sub    16.78 Mankitsu Happening Ep.3 ↔ ep1
                                         15.64 Girls-Rush-1 ↔ Girls-Rush-2
```

Note that several false positives are *different episodes of the same show* —
the case where a wrong "duplicate" verdict is most likely to cost the user a
file.

The alignment recheck is what separates them. Measured on the 15 lowest
reported matches: true duplicates gained `+13.25`, `+17.96`, `+76.33`, while
**every** false positive gained exactly `+0.00`. So the correct policy is
not a lower threshold, it is *always run a cheap alignment and then use a
higher threshold* — which A-01's fix makes affordable.

The wording in `docs/THRESHOLDS.md` and `docs/STATUS.md` should say "0 false
positives **on the corpus**", not "0 false positives".

### A-03 · GUI settings carry the pre-F-24 scale across the upgrade

F-18 (1.7.8) started persisting thresholds through `QSettings`; F-24 (1.8.0)
changed what those numbers mean. Anyone who opened the GUI on 1.7.8 or 1.7.9
has `video_threshold=85` stored. `_restore_settings` reads it back
unconditionally, so after upgrading, 85 is applied on the *calibrated* scale
(raw 92.5) and video matching silently finds nothing. There is no schema
version in the stored settings to detect this.

### A-04 · The local working config still holds pre-F-24 thresholds

`docs/samesame.json` (untracked; points at `S:\Daedalus\Kiroku\Lib\test`)
still contains `"video": 85, "image": 90, "audio": 94`. Only
`samesame.example.json` was migrated. Running with `--config docs/samesame.json`
produces near-zero matches for the same reason as A-03.

---

## High

### A-05 · Cancellation and progress regress inside the video pair loop

`find_video_matches` calls `check_cancelled()` once per *outer* record, and
`fingerprint_video` contains no cancellation checks at all. With the inner
loop now costing seconds per iteration (A-01), one outer iteration on a
300-file library can run for tens of minutes before the token is examined —
which defeats F-02's guarantee that a scan actually stops when cancelled.

The same loop emits no progress events, so the GUI's progress bar and status
line sit frozen for the entire matching phase.

---

## Medium

### A-06 · Image calibration is not scale-preserving, contrary to the docs

**Resolved in 1.9.2, and the fix went further than the finding.** Measured
against real evidence, the colour term was not merely mis-weighted - it was
actively harmful. See "A-06 resolution" below.

`docs/THRESHOLDS.md` states that `image_threshold=80.0` is
`calibrate_similarity(90)` and "preserves the exact pre-F-24 practical
strictness". It does not. Calibration is applied to the structure term
*before* blending, so the two conditions are:

```
before F-24:  0.8·S + 0.2·C >= 90
after  F-24:  0.8·S + 0.1·C >= 80      (equivalent form)
```

The colour term's weight relative to the bar is halved. Worked example:
`S=100, C=0` scores 80 under both, which failed the old bar of 90 and passes
the new bar of 80. Ranking between pairs with different structure/colour
mixes can also flip, so the claim in the F-24 notes that "a monotonic
rescale preserves rank order" does not hold for the image path.

This is a documentation/derivation problem, not necessarily a wrong
threshold — but the number should be re-derived, or the doc corrected to say
the trade-off changed.

### A-07 · Image candidate blocking is wrong for thresholds ≤ 20

In `_candidate_pairs`, `minimum_structure_similarity` is clamped at `0.0`
and *then* decalibrated, and `decalibrate_similarity(0.0)` is `50.0` raw —
so a threshold of 20 or below yields `max_distance = 32` instead of 64. At
those thresholds a pair can legitimately match on colour alone, but the LSH
blocking will never generate it as a candidate. Triggers only when the
candidate set exceeds `max_candidates` (250).

### A-08 · `raw_similarity` in the benchmark output is no longer raw

`image_similarity` and `audio_similarity` calibrate internally, so
`benchmark.py` stores the calibrated value in `LabeledScore.raw_similarity`
for those media types (only video's is genuinely raw). The derived
`raw_min_positive` / `raw_max_negative` fields therefore duplicate the
calibrated ones — misleading precisely when image or audio thresholds are
eventually re-derived from real evidence.

---

## Low

### A-09 · Version drift across the documentation

`README.md` says `1.7.5`, `docs/CONFIG.md` `1.7.5`, `docs/USAGE.md` `1.7.5`,
`docs/GUI_REFERENCE.md` `1.7.7`, `docs/STATUS.md` `1.7.8` in one place and
`1.7.5` in two others — against an actual package version of `1.8.0`.
`docs/STATUS.md`'s "Verification Completed" section also still advertises a
"59-file real collection scan completes in about 20 seconds", which A-01
makes false.

### A-10 · `find_video_matches(duration_tolerance=...)` is dead

The parameter is accepted and never read. (Pre-existing, not introduced by
the recent work.)

### A-11 · Root roll-up can duplicate a directory-level folder pair

When several roots are scanned and files sit directly in a root, the
directory-scope key (`str(path.parent)`) and the root-scope key
(`record.root_key`) are the same string, so the same pair is reported twice
with different `scope` values.

### A-12 · Test-suite global state and minor GUI polish

`tests/test_gui.py` sets `QSettings.setDefaultFormat` / `setPath` in `setUp`
and never restores them in `tearDown`; harmless today because every
`MainWindow()` construction lives in that one class, but it silently becomes
a real-registry write if a future test class constructs a window outside it.
The GUI also restores collection roots without checking that they still
exist.

---

## What is working well

- The F-24 calibration itself: verified zero false positives on a generated
  set of six visually distinct videos, and the video/audio rescale does
  exactly what it documents.
- F-16's directory-level folder comparison with the shared-cluster
  prefilter: 35 folder pairs found on the real library, no quadratic blowup.
- F-20's HTML report: single language, correct per-section counts, working
  filter, no regressions in generated output.
- F-23's clustering and the whole source-protection layer remain untouched
  and green.
- The alignment recheck's *discrimination quality* (A-02) is genuinely
  strong; the problem is only how it is computed.

## Fix plan

| # | Work | Severity | Status |
| --- | --- | --- | --- |
| A-01 | Absolute-time grid fingerprint, cached; alignment without ffmpeg | Critical | Done in 1.9.0 |
| A-02 | Re-derive `video_threshold` on the full library with alignment on | Critical | Done in 1.9.0 — `15.0` → `20.0` |
| A-03 | `QSettings` schema version + reset of pre-F-24 thresholds | Critical | Done in 1.9.0 |
| A-04 | Migrate the local working config | Critical | Done in 1.9.0 |
| A-05 | Cancellation checks and progress events in the pair loop | High | Done in 1.9.0 |
| A-06 | Re-derive or re-document `image_threshold` | Medium | Done in 1.9.1/1.9.2 — formula changed, `80.0` → `60.0` |
| A-07 | Fix LSH blocking for low image thresholds | Medium | Done in 1.9.0 |
| A-08 | Report a genuinely raw score in the benchmark | Medium | Done in 1.9.0 |
| A-09 | Version and claim drift in the documentation | Low | Done in 1.9.0 |
| A-10 | Remove the dead `duration_tolerance` parameter | Low | Done in 1.9.0 |
| A-11 | De-duplicate identical folder pairs across scopes | Low | Done in 1.9.0 |
| A-12 | Restore Qt global state in tests; validate restored roots | Low | Qt state done in 1.9.0; root validation open |
| A-13 | Grid cadence too coarse to resolve a few seconds of offset | Critical | Done in 1.9.1 |
| A-14 | Audio: 1-second alignment, 3-second duration gate, threshold above where real duplicates land | Critical | Done in 1.9.3 |

## A-13 · The 1.9.0 grid cadence was too coarse to see a few seconds of offset

Found while investigating F-30 (the "descriptor ceiling") with the fast
infrastructure A-01 had just made possible. **It was not a descriptor
problem, and F-30's premise does not hold for these pairs** — it was a defect
in A-01's own fix.

A-01 chose the grid cadence to bound the sample count: a power-of-two ladder
targeting ~96 samples, which for a 28-minute episode means one frame every
**32 seconds**. But an integer-slot shift can only cancel offsets that are
*multiples of the cadence*, and a few seconds of extra logo, intro, or black
at the head of a release is the most common difference between two copies of
one episode. At a 32-second cadence such a pair is invisible: every sample
lands in a different shot, and the pair scores as noise.

Measured on `Bikyaku Seido Kaichou Ai 01`, `_dub` versus `_rus` — which the
1.9.0 matcher scored `19.08`, below its own threshold. A fine cross-scan of
frames 60-360s found a clean constant offset:

```
best constant offsets (mean similarity over overlapping frames):
  offset     +6s  mean  81.4%  over 49 frames     <- the true alignment
  offset    +12s  mean  57.2%  over 48 frames
  offset     +0s  mean  54.8%  over 50 frames     <- what a 32s grid can see
frame pairs >= 85%: 38 of 2500, many at exactly 100%
```

Six seconds. Invisible to a 32-second grid, obvious at a fine one.

The reason A-01 picked a coarse cadence was cost: one ffmpeg seek per sample
at ~0.16 s means 425 seeks (68 s) for a 4-second cadence. But that trade
inverts once the sample count is large — the 1.7.0 audit's withdrawn
"single decode pass" idea was measured for **15** samples, where seeks
rightly won. Re-measured on a 28-minute 720p episode for a *grid*:

| Approach | Samples | Time |
| --- | ---: | ---: |
| Seeks, 32s cadence (1.9.0) | 53 | 8.5 s |
| Seeks, 4s cadence | 425 | 67.8 s (extrapolated) |
| **One decode pass, 4s cadence** | 426 | **7.5 s** |
| **One decode pass, 2s cadence** | 851 | **7.9 s** |

One pass at a 2-second cadence is *cheaper* than 53 seeks and gives 16×
the resolution, because ffmpeg does the downscale and the cost is decode-
bound, not sample-bound.

**Fix (1.9.1):** fixed 2-second cadence for every file, built in a single
decode pass (`fps=1/2,scale=32:32`, gray rawvideo). A fixed cadence also
removes the power-of-two ladder and the cross-cadence striding it needed.
The shift search became coarse-to-fine with a bounded probe subsample, since
a flat scan of ~180 shifts against ~850 slots would be ~76k comparisons per
pair.

Effect on the labelled pairs — true positives move, false positives do not:

| Pair | Truth | 1.9.0 | 1.9.1 |
| --- | --- | ---: | ---: |
| `Bikyaku Seido Kaichou Ai 01` `_dub` ↔ `_rus` | duplicate | 19.08 | **66.30** |
| `Vampire_02` ↔ `Vampire_02` | duplicate | 15.25 | **75.42** |
| `Vampire_01` ↔ `Vampire_01` | duplicate | 21.50 | **71.72** |
| `Mesudachi-The-Animation-2` ↔ `Мои горячие подружки 2` | duplicate | 91.98 | 92.87 |
| `Kimekoi Ep.2` ↔ `Shishunki-Sex-1` | unrelated | 15.47 | 15.47 |
| `Venus Blood Brave/Ep.2` ↔ `Boku Dake/Ep.1` | unrelated | 15.44 | 15.44 |
| `Aku no Onna Kanbu` Ep.1 ↔ Ep.2 | different episodes | 17.33 | 17.33 |
| `Girls-Rush-1` ↔ `Girls-Rush-2` | different episodes | 15.64 | 15.64 |

Re-measured over the whole library afterwards, the two classes now separate
with a wide empty gap — **no pair at all scores between 18.72 and 28.44**.
Everything above the gap is a genuine same-title duplicate; everything below
it is cross-title. The `20.0` default therefore sits inside empty space
rather than on a knife edge, and needed no change.

| Library scan | 1.9.0 (32s grid) | 1.9.1 (2s grid) |
| --- | ---: | ---: |
| Video matches at the `20.0` default | 105 | **129** |
| Lowest labelled true positive | 15.25 | **34.00** |
| Highest labelled false positive | 19.21 | **18.72** |
| Grid build, 276 files, 4 workers | 15 min | 26 min |
| Warm-cache scan | 32 s | 105 s |

The extra 24 matches are true duplicates that 1.9.0 scored as noise. The
cost is a slower one-time grid build (it reads every file once, so it is
disk-bandwidth-bound) and a 3.5× slower pair loop, both of which stay well
inside a usable scan.

Two process notes worth keeping:

- **F-30 was chasing the wrong cause.** Both of its named misses were
  alignment failures, not descriptor ceilings. The 1.7.0 audit's reasoning
  ("one episode has essentially no duration delta, yet still scores low,
  which rules out alignment") assumed alignment means *duration*
  compensation; a same-duration pair can still be offset in time.
- **An intermediate conclusion here was wrong and caught by measurement.**
  An order-agnostic coverage check said these files shared almost no frames
  (5.7%), which looked like proof of different content — but the check
  sampled both files on the same broken grid, so it inherited the same blind
  spot. The contact sheets and the fine cross-scan corrected it.

## A-06 resolution — the colour term was harmful, not just mis-weighted

A-06 recorded that `image_threshold=80` was documented as scale-preserving
and was not. Deriving a correct number needed real image evidence, which the
project did not have. Built one: **132 real images** (110 frames sampled
across different episodes of the library, plus all 22 real posters in it),
**720 true positives** produced by the transformations the tool claims to
support (½ and ¼ resize, JPEG q80/q40, WebP, BMP, resize+recompress,
brightness ±20%, saturation ±40%, contrast +25%), and **5 995 unrelated real
pairs**.

The first measurement invalidated the formula's central assumption. The code
said the colour term "is a direct RGB distance and is already meaningful on
0-100". On real images it is not:

```
UNRELATED real image pairs (n=8646):
  structure, calibrated     median   0.00   p95  25.00
  colour term               median  83.66   p95  96.47
  blended score             median  18.73   p95  34.64
```

Unrelated images share an average colour far more often than they share
structure, so the colour term has a *worse* noise floor than the one F-24
was written to fix — it contributed ~17 points to every pair.

Three formulations, same corpus:

| Formulation | Worst true positive | Worst false positive | Gap |
| --- | ---: | ---: | ---: |
| Blend, colour uncalibrated (shipped in 1.9.1) | 77.86 | 54.03 | +23.82 |
| Blend, colour calibrated at its own noise floor | 66.60 | 48.95 | +17.65 |
| **Structure only** | **75.00** | **43.75** | **+31.25** |

Calibrating colour — the obvious fix, and what A-06 implied — is the *worst*
of the three: once colour is stretched against its floor, ordinary colour
grading reads as a real difference and graded duplicates collapse to 66.60.

Dropping colour outright was better on every axis except one, and the
existing test suite found that one immediately: two solid colours have no
structure to compare, and their perceptual hashes are near-identical (red vs
white: 81.25). So colour is kept, but demoted to a **veto** — it can reject a
pair, never lift one. That removes its noise-floor problem by construction.
The bound is evidence-derived too: real duplicates never fall below 83.27
colour similarity (worst case a 20% brightness grade), solid-colour pairs
reach at most 66.67, so the veto sits at 70 and rejects 0 of 720 true
positives while rejecting all 10 solid-colour pairs.

Final: `image_similarity` returns calibrated structure with a colour veto,
and `image_threshold` moves `80.0` → **`60.0`**, the midpoint of the
75.00/43.75 gap — 0 false positives and 0 false negatives on the corpus.

A process note: this is the second finding in this audit where the *stated
reason* for a design turned out to be untested (the first was A-13's
assumption about alignment). Both survived because they sounded right. The
corpus took about twenty minutes to build.

## A-14 · Audio alignment had the same blind spot, and its threshold missed 80% of real duplicates

Opened after A-13 and A-06, on the suspicion that `audio_threshold` was the
last default still carried over from the pre-F-24 scale rather than measured,
and that `MAX_ALIGNMENT_SHIFT = 8` looked like A-13's problem in another
medium. Both were true, and there was a third problem underneath.

ffmpeg's Chromaprint muxer emits one item per **0.1266 s**, which makes the
constants legible for the first time:

```
MAX_ALIGNMENT_SHIFT = 8 items   = 1.01 seconds of shift
duration_tolerance  = 3 seconds - pairs outside it are never compared at all
minimum_overlap     = 90% of the shorter fingerprint
```

Against video's ±180 s of shift and 15-minute duration gating, audio was
running on gates an order of magnitude tighter.

**Three findings, all measured on the library's own audio.**

*One second of shift is not enough.* On 30 real 120-second segments, every
codec, bitrate, volume, channel and sample-rate transformation the tool
claims to support scores 96.58-100. A **2-second lead-in scores 8.20-10.90**:

| Transformation | min | median |
| --- | ---: | ---: |
| mp3 320k / 128k / 96k | 99.68 / 98.04 / 96.60 | 99.91 / 98.85 / 97.71 |
| aac 128k, opus 96k, flac | 96.96 / 96.58 / 100.00 | 98.79 / 97.68 / 100.00 |
| volume −6 dB / +4 dB, mono, 22 kHz | 99.50-99.93 | ~99.99 |
| lead-in 0.5 s | 98.10 | 98.63 |
| **lead-in 2 s** | **8.20** | **10.90** |
| **lead-in 5 s** | **5.95** | **8.09** |

*The threshold was far above where real duplicates land.* Taking the audio
of 20 video pairs the (fixed) video matcher confirms as duplicates, against
40 unrelated pairs, all full-length:

```
positives  min 30.46   p10 82.28   median 85.58
negatives  max  6.83   p90  6.28   median  5.68
```

At the shipped `88.0`, **16 of the 20 confirmed pairs were missed**. The
`30.46` outlier is not a miss: it is `Youkoso-Sukebe-Elf-no-Mori-e_01_rus`
against its non-dubbed release — same video, genuinely different audio, and
scoring it low is the audio channel doing its job as independent evidence.
Excluding it, real duplicate audio starts at `80.87` against a negative
ceiling of `6.83`.

*The duration tolerance silently excluded pairs.* Two of the 20 pairs differ
in length by 36.7 s and 82.7 s, so at a 3-second tolerance they were never
compared. One of them scores `82.68` once it is.

**Fixes (1.9.3):** shift range 8 → 237 items (30 s), duration tolerance
3 s → 120 s, minimum overlap 90% → 75%, `audio_threshold` 88.0 → **60.0**
(0 false positives and 0 false negatives on the evidence, with 53 points of
margin above the negative ceiling and 21 below the positive floor).

**And the same cost trap as A-01, avoided by measuring first.** Widening the
shift naively is O(range × overlap): measured at 240 ms per pair, i.e. 91
minutes for a library-sized pass. The search now subsamples the *overlap*
(64 probes per shift) and scores only the winning shift exactly, which lands
at **3.1 ms per pair — three times cheaper than the old one-second search**
while covering thirty times the range.

One design note worth keeping: the first implementation also strided the
shift search coarsely, which assumes the score varies smoothly around the
true offset. A new regression test with random fingerprints failed
immediately, because that peak can be one item wide. Real Chromaprint items
are correlated in time so it would have worked in practice and hidden the
assumption — the stride was removed rather than the test weakened.

**A fourth problem, surfaced by the existing suite.** Lowering the threshold
made `test_cli_matches_wav_mp3_flac_and_rejects_unrelated_audio` fail: its
"unrelated" file started matching. The cause is the exact audio counterpart
of A-06's flat-image problem — a pure tone fingerprints to a **single
repeated Chromaprint item**, so two *different* tones score `75.00` against
each other. That was true before this work too; `88.0` merely hid it.

```
                        distinct items    two different tones score
real library audio      99.1% - 99.6%     -
pink noise              96.4%             51.18 vs white noise
silence / pure tone      0.7%             75.00
```

So `audio_similarity` now vetoes a fingerprint whose distinct-item ratio is
below 50% — real content sits at 99%+, degenerate content at 0.7%, so the
bound has two orders of magnitude of margin.

The test fixture itself was the degenerate case: it built its sources from
pure tones, which means its positive assertions had been passing for the
same reason its negative assertion nearly failed. It now mixes seeded pink
noise with the tones (`source vs mp3` 97.59, `source vs flac` 100.00,
`source vs unrelated` 41.07), so both directions are meaningful.

**Scope, stated plainly.** `find_audio_matches` only ever receives files
with *audio* extensions, so a video-only library never exercises the audio
path at all — a scan of `TestLib` reports zero audio matches and skips the
stage in 0.3 s. The evidence above is real audio (the library's own tracks,
and real Chromaprint fingerprints of them), which is exactly what the
matcher consumes, but this library does not exercise the path end to end.
That was verified separately on a 20-file library of real audio files: 29
matches, all correct — 16 same-track across wav/mp3/flac, and 13 from two
episodes that genuinely exist twice in the library. Pairs offset by 3
seconds score 91.49-91.73, where before they were unreachable.

Whether video files should also contribute audio evidence is a scoping
decision, not a defect, and is not changed here.

## Verification of the 1.9.0 fixes

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **130 passed, 7 subtests** (5 new A-01/A-03 regressions) |
| `ruff check .` / `ruff format --check .` | clean |
| `mypy dedupe` | 33 errors (was 37 — the removed re-decode path took four with it) |
| Full library scan, cold cache | 1017 s, 105 video matches at the new `20.0` default |
| Full library scan, warm cache | 31.7 s |
| Alignment cost | 1.34 ms/pair over all 22 849 compatible pairs |

The new regressions cover the properties the design depends on rather than
the numbers it produced: that the cadence ladder stays on powers of two,
that a coarser grid is exactly a stride of a finer one, that a time-shifted
duplicate aligns, that a failed frame stays a placeholder instead of
shifting the sequence, that an unreadable file gives up after a few probes,
and that thresholds from an older settings schema are not restored.
