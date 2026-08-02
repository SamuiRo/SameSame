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

**Proposed fix:** sample each file once on an *absolute* time
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
| A-01 | Absolute-time grid fingerprint, cached; alignment without ffmpeg | Critical | Planned for 1.9.0 |
| A-02 | Re-derive `video_threshold` on the full library with alignment on | Critical | Planned for 1.9.0 |
| A-03 | `QSettings` schema version + reset of pre-F-24 thresholds | Critical | Planned for 1.9.0 |
| A-04 | Migrate the local working config | Critical | Planned for 1.9.0 |
| A-05 | Cancellation checks and progress events in the pair loop | High | Planned for 1.9.0 |
| A-06 | Re-derive or re-document `image_threshold` | Medium | Open — needs real image evidence |
| A-07 | Fix LSH blocking for low image thresholds | Medium | Planned for 1.9.0 |
| A-08 | Report a genuinely raw score in the benchmark | Medium | Planned for 1.9.0 |
| A-09 | Version and claim drift in the documentation | Low | Planned for 1.9.0 |
| A-10 | Remove the dead `duration_tolerance` parameter | Low | Planned for 1.9.0 |
| A-11 | De-duplicate identical folder pairs across scopes | Low | Planned for 1.9.0 |
| A-12 | Restore Qt global state in tests; validate restored roots | Low | Open — cosmetic |
