# Project Audit — 1.10.1

Audit date: 2026-08-03
Audited version: `1.10.1` (`master` @ `ac3dc94`, clean working tree)

This audit follows `docs/AUDIT-1.8.0.md` and covers everything done after it:
1.9.0 (`7c8944c`, A-01..A-05/A-07..A-09/A-11/A-12), 1.9.1 (`e069acb`, A-13),
1.9.2 (`b29eca1`, A-06), 1.9.3 (`706afe4`, A-14), 1.9.4 (`66e1c14`, A-12),
1.10.0 (`3b8785f`, A-15) and 1.10.1 (`ac3dc94`, A-16).

Severity uses the same scale: **Critical** = the feature does not work at real
scale; **High** = user-visible malfunction or serious slowdown; **Medium** =
correctness gap with a narrow trigger; **Low** = hygiene.

Findings continue the `A-` numbering at **A-17**.

## Method

Read in full: `video_fingerprint`, `image_fingerprint`, `audio_fingerprint`,
`cache`, `models`, `service`, `scanner`, `exact_hash`, `folder_compare`,
`progress`, `db`, `benchmark`, `thresholds`, `report`, `cli`,
`actions/service`, and `gui/main_window`, plus the full diff since `55d6e70`.

Baseline checks on the project `.venv` (Python 3.11.9):

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **145 passed, 14 subtests** in 18 s |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 63 files already formatted |
| `mypy dedupe` | 33 errors — unchanged documented baseline |
| Full `TestLib` scan, cold cache | 2192 s, **every documented number reproduced**, 0 warnings |
| Full `TestLib` scan, warm cache | **105.5 s** (STATUS.md: 105 s), identical results |
| Package/doc version agreement | consistent at `1.10.1` (A-09 stayed fixed) |

Every finding below except A-25 and A-26 (both hygiene, established by
reading) was **measured** rather than inferred; the probe output is quoted
inline.

## Summary of outcome

The 1.9.x/1.10.x work holds up on the axes it was derived for. A cold-cache
scan of the real library reproduces **every** number `docs/STATUS.md` records
— 14 exact groups, 129 video matches, 61 folder pairs, 4 name hints, 105.5 s
warm — with no warnings, and the lowest reported video match is exactly the
`28.44` that A-13's empty-gap analysis predicted. The alignment grid, the
image formula, the audio gates and the dihedral views all do what their
entries in `docs/AUDIT-1.8.0.md` claim, and the source-protection layer
(`actions/`, `consolidation.py`, `transcode/`) is untouched since the 1.8.0
audit's baseline and still sound.

The problems are four, and three of them are the same problem wearing
different clothes — a constant that was correct when it was written and was
never re-derived when the thing it guards moved:

1. **The A-03 guard was not maintained.** `SETTINGS_SCHEMA_VERSION` was set to
   `2` in 1.9.0 and never bumped, but 1.9.2 and 1.9.3 both changed what an
   image/audio threshold *means*. A GUI profile written by 1.9.0 or 1.9.1
   restores `image_threshold=80` and `audio_threshold=88` on today's build —
   the exact failure A-03 exists to prevent (A-17).
2. **A circuit-breaker stopped breaking anything when a threshold moved.**
   A-06 lowered `image_threshold` 80 → 60; at 60 the LSH candidate blocking
   generates **100 % of all pairs** on unrelated content, so past
   `max_candidates` it is a quadratic memory hazard rather than a prefilter
   (A-18). This is A-01's `ALIGNMENT_RECHECK_FLOOR` again, in another medium.
3. **A gate widened on 20 files behaves differently on 800.** A-14 moved the
   audio duration tolerance 3 s → 120 s; the bucket sweep around it is
   quadratic, and pair *generation alone* goes from 0.02 s to 15.6 s at 800
   files (A-19).

The fourth is unrelated and older: the 26-minute alignment-grid build is one
uncommitted sqlite transaction, so a cancel or a crash at minute 25 keeps
nothing (A-20).

Everything else is smaller.

---

## High

### A-17 · The settings-schema guard was not bumped when thresholds changed meaning again

A-03 added `SETTINGS_SCHEMA_VERSION` precisely so that stored thresholds from
an older *scale* are dropped rather than restored. It was set to `2` in 1.9.0
(`dedupe/gui/main_window.py:51`) and has not moved since. But:

| Version | What changed | Default |
| --- | --- | ---: |
| 1.9.2 (A-06) | image scoring formula — colour became a veto | `80.0` → `60.0` |
| 1.9.3 (A-14) | audio shift/overlap/duration gates + degenerate veto | `88.0` → `60.0` |

Both are exactly the kind of change the guard exists for, and neither bumped
it. Measured, by writing the settings a 1.9.0/1.9.1 user would have and
constructing today's `MainWindow`:

```
SETTINGS_SCHEMA_VERSION = 2
restored video_threshold = 20.0  (shipped default 20.0)
restored image_threshold = 80.0  (shipped default 60.0)
restored audio_threshold = 88.0  (shipped default 60.0)
```

The consequences are already quantified by this audit series' own evidence:

- **Audio at 88.0**: A-14 measured 20 user-confirmed duplicate pairs against
  40 unrelated ones; at `88.0`, **16 of the 20 were missed**. Real duplicate
  audio starts at `80.87`.
- **Image at 80.0**: A-06 measured 720 transformed duplicates; the worst true
  positive on the *current* structure-only formula is `75.00`, so `80.0`
  rejects genuine duplicates outright — and it is a bar the old blended
  formula's numbers, not this one's, were chosen against.

`tests/test_gui.py:82` covers only the schema-0 case (nothing stored). There
is no test that a stored value from *the current schema number* is still
meaningful, which is why two threshold redefinitions passed through unnoticed.

**Fix:** bump `SETTINGS_SCHEMA_VERSION` to `3`, and make the constant's
docstring state the rule — *bump this whenever a threshold's scale, formula,
or default changes* — so the next redefinition cannot forget. Consider
storing the shipped default alongside each threshold and resetting any value
that equals a superseded default.

### A-18 · Image candidate blocking has no selectivity at the shipped threshold

`_candidate_pairs` decalibrates the threshold and derives a Hamming bound:

```
image_threshold 60.0 -> raw 80.0 -> max_distance 12 -> 13 blocks of ~4.9 bits
image_threshold 80.0 -> raw 90.0 -> max_distance  6 ->  7 blocks of ~9.1 bits
image_threshold 90.0 -> raw 95.0 -> max_distance  3 ->  4 blocks of ~16 bits
```

At ~5 bits a block has 32 possible values, so every bucket holds roughly an
eighth of the corpus and the pigeonhole prefilter degenerates. Measured on
random, mutually unrelated fingerprints (so the *correct* answer is "almost no
candidates"):

```
n=  300 threshold= 60.0  candidates=    44850 (100.00% of all)  in  0.10s
n=  300 threshold= 90.0  candidates=      166 (  0.37% of all)  in  0.01s
n= 1200 threshold= 60.0  candidates=   719400 (100.00% of all)  in  3.09s
n= 1200 threshold= 90.0  candidates=     2826 (  0.39% of all)  in  0.11s
n= 2400 threshold= 60.0  candidates=  2878800 (100.00% of all)  in 13.46s
n= 2400 threshold= 90.0  candidates=    11295 (  0.39% of all)  in  0.52s
```

It is not merely useless — it is *more expensive* than not blocking, because
each fingerprint is indexed under 8 orientations × 13 blocks and the same pair
is generated many times before the set collapses it.

Selectivity collapses steeply, and well above the shipped value (n = 1200
unrelated fingerprints, so every candidate generated is waste):

| `image_threshold` | candidates | of all pairs |
| ---: | ---: | ---: |
| 90.0 | 2 819 | 0.39 % |
| 80.0 (pre-A-06 default) | 400 045 | 55.61 % |
| 70.0 | 719 161 | 99.97 % |
| **60.0 (shipped)** | **719 400** | **100.00 %** |

Note that the pre-A-06 default of `80.0` was already only half-selective; A-06
did not create the problem so much as walk it off the end of the cliff.

Memory is the real hazard. The candidate set is materialised in full before
any scoring:

```
n=2000: 1 999 000 candidate pairs, peak 181 MB for the set alone
image_similarity: 9.9 us/pair
extrapolated n= 5 000:  12 497 500 pairs, ~1.1 GB for the set,  2 min scoring
extrapolated n=20 000: 199 990 000 pairs, ~18 GB for the set,  33 min scoring
```

So `max_image_candidates=250` currently reads as "above 250 images, switch
from an honest O(n²) loop to a more expensive O(n²) loop that also allocates
O(n²) memory". A 20 000-image folder — an ordinary photo library, and the
case the blocking was written for — exhausts memory before it scores anything.

Nothing re-measured this when the threshold moved. A-07 (1.9.0) touched the
very same clamp for correctness at low thresholds, three commits before A-06
lowered the threshold into the range where the *cost* argument fails.

**Note why the full-library rule did not catch this.** `TestLib` holds 22
images — below `max_candidates=250` — so every library scan takes the
all-pairs branch and the blocking code never executes. The project's own
"validate on the real library" discipline is right and is not at fault here:
the library simply has no image corpus. Image-path changes need a
*large-image* fixture as well, and this is the second finding in this series
(after A-16's untested `samesame-benchmark`) that lived in a branch nothing
ran.

**Fix:** the blocking scheme has to survive a 12-bit radius. The standard
answer is banded LSH over multiple independent bit samples (r bands of b bits,
tuned so the false-negative probability at 12 bits is negligible) rather than
a single pigeonhole partition; failing that, stream candidate pairs instead of
materialising a set, and cap by bucket size. Whatever is chosen must be
measured at the shipped threshold, not at the one the code was written for.

### A-19 · Audio's widened duration tolerance made the bucket sweep quadratic

A-14 moved `DEFAULT_DURATION_TOLERANCE` from `3.0` to `120.0` on the evidence
of a 20-file audio library. The sweep around it was not revisited: for every
distinct integer duration, `find_audio_matches` gathers everything within
±tolerance and enumerates *all pairs of that list*, de-duplicating through a
`seen` set. Widening the window by 40× multiplies how many times each pair is
regenerated.

Measured with scoring stubbed out, so these are pure loop-overhead numbers:

```
n=200 spread=300s  tolerance=  3s   398 pairs scored, loop  0.001s
n=200 spread=300s  tolerance=120s  12738 pairs scored, loop  0.367s
n=400 spread=300s  tolerance=  3s  1648 pairs scored, loop  0.006s
n=400 spread=300s  tolerance=120s  50902 pairs scored, loop  2.673s
n=800 spread=300s  tolerance=  3s  6372 pairs scored, loop  0.022s
n=800 spread=300s  tolerance=120s 205801 pairs scored, loop 15.643s
n=800 spread= 60s  tolerance=120s 319600 pairs scored (all), loop  8.364s
```

Two separate costs:

- **Comparisons.** 800 tracks now score 205 801 pairs (64 % of all pairs)
  where they scored 6 372. At A-14's measured 3.1 ms/pair that is ~11 minutes
  for 800 tracks; a 5 000-track music library is several hours.
- **Overhead.** 15.6 s of the above is spent building and de-duplicating pair
  tuples, before any fingerprint is touched.
- **Memory.** `seen` holds one `(str, str)` tuple of full paths per generated
  pair and is never cleared. It is 206 k tuples at 800 files / 300 s spread
  and grows with the square of the library — the same O(n²) materialisation
  as A-18, in a second matcher.

The widening itself is right (A-14's evidence for it is sound); the sweep
around it is what needs replacing. The video path already solves this exact
problem — sort by duration and break at the first incompatible candidate, the
sorted sweep F-01 introduced and the comment in `find_video_matches` explains
— and the audio path was left on the older bucket scheme.

**Fix:** reuse the video path's sorted-sweep-and-break. It generates every
unordered pair exactly once, needs no `seen` set at all, and makes the
tolerance a cheap parameter instead of a quadratic multiplier.

### A-20 · A whole fingerprinting stage is one uncommitted transaction

`Cache.upsert_file` never commits; each stage in `find_video_matches` /
`find_image_matches` / `find_audio_matches` commits once, after the stage
finishes. The alignment-grid build is the longest operation the product
performs — 26 minutes for the real library, and it reads every byte of it —
and it is a single open transaction for that entire time.

Observed directly during this audit's full-library scan: the WAL file stopped
growing at 16:01 and did not change again for the whole grid stage. Confirmed
in isolation with a stubbed grid builder and a cancel three-quarters of the
way through:

```
cancelled after building 40 of 40 grids
grids surviving in the cache after cancellation: 0
```

`docs/STATUS.md` advertises that cancellation "retains already committed cache
work", which is literally true and practically misleading: for the one stage
where it matters, nothing is committed until the end. A user who cancels a
first scan at minute 25 of 26 — or loses power, or hits a crash — repeats the
entire read of the library.

A second, related gap: `_ensure_grid_fingerprint` returning `None` is not
recorded anywhere, so a file whose grid cannot be built is retried on **every
subsequent scan**, each attempt a fresh full decode under a 600 s timeout.

**Fix:** commit periodically inside the long stages (every N completed
futures, or every few seconds) so cancellation keeps what has been paid for;
and persist a negative result for a failed grid so it is not re-attempted
until the file changes.

### A-28 · Undestroyed Qt windows can abort the process when they are collected

Found while fixing A-17, by adding two `MainWindow()` constructions to
`tests/test_gui.py` — a change that should have been inert.

> **Correction.** This finding first recorded the trigger as the *number* of
> windows ("+2 windows → 7 of 8 runs crash"). That was wrong, and a follow-up
> bisect disproved it. The count is irrelevant; the **position in the test
> order** is what matters. The corrected mechanism is below. The original
> reasoning was a plausible story that fit the first measurement, and it
> survived exactly one experiment designed to test it.

`close()` hides a window; it does not destroy it. The GUI suite builds 18
`MainWindow` instances and closes all of them, so 18 windows survive, each
holding the two `QMediaPlayer` / `QAudioOutput` / `QVideoWidget` sets its
preview pane creates. They are freed only when CPython eventually collects
them — on whatever thread happens to be collecting.

What the bisect actually established:

| Configuration | Full runs |
| --- | --- |
| Pre-change suite | 8 pass, 0 crash |
| The two new tests at their alphabetical positions | 1 pass, 5 crash |
| **The same two tests renamed to sort last** | **6 pass, 0 crash** |
| Two *plain* extra windows appended at the end | 6 pass, 0 crash |
| Only the earlier-sorting of the two, in place | 6 pass, 0 crash |
| Only the later-sorting one, in place | crashes |

So a single extra window is enough — but only in one slot: immediately after
`test_similar_video_chain_is_one_review_case_with_working_batch_cleanup`,
which is the test that drives a `QMediaPlayer` at a `.mkv` containing junk
bytes and then re-selects those paths after quarantining them.

The crash is a **Windows fatal exception: access violation**, and it lands in
`tests/test_images.py` — a file that has nothing to do with Qt:

```
Thread A: PIL/Image.py load <- transpose <- rotate
          <- image_fingerprint._orientations   (a PIL worker thread)
Thread B: Garbage-collecting
          PIL/Image.py convert <- video_fingerprint.phash   (another worker)
Extension modules: PIL._imaging, ..., PySide6.QtMultimedia, ...
```

That is shiboken finalizing Qt multimedia objects on a PIL fingerprinting
worker thread. Destroying Qt objects off the GUI thread is undefined
behaviour.

The obvious countermeasure — force the collection onto the GUI thread in
`tearDown` — was tried and **made it deterministic**: 10 of 10 runs aborted,
now inside `gc.collect()` in `tearDown` itself. So the problem is not only
*which thread* finalizes these objects; finalizing them at all, in the state
`close()` leaves them in, is unsafe. That result is worth recording precisely
because it refutes the fix that looks right.

**What reproduction was tried and failed.** Four standalone probes, each run
several times, all survived:

```
20 MainWindows built + closed + dropped, then gc.collect()          survives
20 MainWindows, then 60 images fingerprinted on 4 PIL threads       survives
window -> MediaPane._load_path(junk .mkv) -> close -> new window    survives
the same with a missing .mkv, and with no media at all              survives
```

So it is not "leaked windows" alone, not "leaked windows plus PIL threads",
and not the bogus-media path on its own. It needs the accumulated state of
the full suite — the scan-worker `QThread`s, the transcode queue, the journal
connections — and only then does one extra window in one position tip it over.
`MediaPane._load_path` already guards a missing file, so that is not the gap.

**What is proven and what is not.** Proven: at a specific point in this
process's Qt object graph, constructing one more `MainWindow` leads to a
native abort later, in unrelated code, and forcing collection onto the GUI
thread makes it deterministic rather than fixing it. Not proven: any of the
usual explanations. The count hypothesis is disproved; the "wrong thread"
hypothesis is disproved by the `gc.collect()` result; the bogus-media
hypothesis did not reproduce in isolation.

Not fixed here, deliberately, and now for a stronger reason than scope: the
first plausible fix was refuted by measurement, and the second (deterministic
destruction) made things worse. Root-causing this means bisecting Qt object
lifetimes across eighteen tests' accumulated state, which is its own piece of
work. Guessing at a fix without that would be exactly the mistake this audit
series keeps recording.

**Practical guard rail meanwhile:** `tests/test_gui.py` carries a note that a
new `MainWindow`-constructing test should be named to sort *late* in the
class until this is understood. That is a workaround, not a fix, and it is
fragile by construction — it depends on alphabetical ordering.

**Fix, when someone takes it:** start from the reproduction recipe above
(insert a window-constructing test immediately after
`test_similar_video_chain_...` and run the full suite ~6 times). Bisect by
removing preceding GUI tests until it stops, which identifies the object
graph that matters. Only then choose between deterministic destruction,
detaching the media players earlier, or isolating the GUI suite in its own
process.

---

## Medium

### A-21 · The documented nested config silently sets `audio_threshold` to 88

`docs/CONFIG.md`'s nested-format example contains both:

```json
"matching": { "video": 20, "image": 60, "audio": 60, ... },
...
"audio":    { "skip": false, "threshold": 88 }
```

`_flatten_config` writes keys in document order and the later one wins.
Verified by running the documented example through it:

```
block 2: audio_threshold -> 88 | matching.audio was 60 | audio.threshold was 88
```

So a user who copies the documented config gets exactly the threshold A-14
measured as missing 16 of 20 confirmed duplicates. `docs/samesame.example.json`
and the working `docs/samesame.json` were both migrated; only the inline
example in the prose was missed.

**Fix:** correct the example to `60`. Separately, `_flatten_config` resolving
two spellings of the same setting by document order is a trap worth closing —
either make `matching.*` authoritative, or reject a config that sets the same
threshold twice with different values.

### A-22 · The benchmark still models a matcher gate that no longer exists

`dedupe/benchmark.py:313` gates audio pairs on

```python
if duration_delta > 3.0:
    ...  note = f"duration delta {duration_delta:.3f}s exceeds matcher tolerance 3.0s"
```

`DEFAULT_DURATION_TOLERANCE` has been `120.0` since 1.9.3. The benchmark
therefore reports pairs as ineligible that a real scan compares happily, and
its note states a tolerance the tool does not have. A-16 moved the thresholds
into `DEFAULT_CONFIG` for exactly this reason and left this literal behind;
`tests/test_benchmark_integration.py` cannot catch it because every synthetic
audio pair has identical durations.

Two smaller drifts in the same file:

- `raw_similarity = None` for images, with a comment explaining that
  `image_similarity` "blends a calibrated structure term with an uncalibrated
  colour term, so the result has no single raw-scale counterpart". Since A-06
  the score is *purely* calibrated structure, so — exactly as with audio — the
  raw value is recoverable through `decalibrate_similarity`. A-08's fix is
  half-applied.
- The video path's duration gate correctly imports
  `video_durations_compatible`; the audio path should import the tolerance the
  same way rather than repeating a number.

### A-23 · Border trimming has a 40 % cliff that two copies of one picture can straddle

`trim_border` refuses to trim when the detected content box is under
`MINIMUM_TRIMMED_AREA` (0.4) of the image. That decision is taken
independently per file, from a JPEG-noisy probe, so two copies of one picture
can land on opposite sides of it — and a trimmed descriptor has nothing in
common with an untrimmed one.

Measured on generated pictures with a large uniform field (source PNG versus
its half-size q40 JPEG), against the same pair scored with trimming disabled:

| content area | source trim box | jpeg trim box (×2) | shipped | trimming off |
| ---: | --- | --- | ---: | ---: |
| 0.30 | (600, 400) | (600, 400) | 93.75 | 93.75 |
| 0.38 | (600, 400) | (600, 400) | 87.50 | 87.50 |
| **0.40** | **(600, 400)** | **(312, 312)** | **6.25** | **87.50** |
| 0.42 | (317, 317) | (322, 320) | 31.25 | 87.50 |
| 0.46 | (332, 332) | (338, 334) | 62.50 | 93.75 |
| 0.55 | (363, 363) | (366, 366) | 68.75 | 87.50 |
| 0.70 | (409, 400) | (410, 400) | 81.25 | 81.25 |

Two distinct effects: the cliff at 0.40 (one copy trims, the other does not —
a real duplicate scores `6.25`), and, above it, trim boxes that differ by a
handful of pixels between the two copies and cost 20–55 points.

**This is a latent gap, not a regression on current evidence.** Re-running
A-15's method on the library's own 22 real posters shows trimming is a clear
win there and costs nothing:

```
                       trim on (shipped)        trim off
letterbox (+25% h)  min  87.50, 0/22 missed   min 31.25, 16/22 missed
white margin 8%     min 100.00, 0/22 missed   min 12.50, 20/22 missed
half / q40 / webp / brightness / rotate 90: identical either way
```

Real posters are full-bleed artwork, where the trim box is stable. The failure
class is pictures where a uniform field is most of the frame — cover art with
wide margins, scanned pages, logos, product shots — none of which are in the
current corpus.

**Fix:** the cheapest robust answer is to stop making the decision per file:
store the untrimmed views *and* the trimmed views and take the best match, so
a disagreement about whether to trim can no longer zero a pair. That doubles
the fingerprint; it must be measured against the negatives before shipping,
because A-15 rejected a 16-view descriptor (an 80 %-centre crop) for pushing
the worst unrelated pair from 43.75 to 56.25 — but note an *untrimmed whole
image* is a different addition from a centre crop and deserves its own
measurement rather than inheriting that verdict.

---

## Low

### A-24 · The video pair loop is still O(n²) and single-threaded

A-01 fixed the constant, not the shape. Measured on realistic 750-slot grids
(a 25-minute episode at the 2 s cadence):

```
grid_aligned_similarity: 5.82 ms/pair   (measured)

pair count x 5.82 ms, single-threaded:
  276 videos,  22 849 compatible pairs (measured) ->   2 min
1 000 videos, ~300 000 compatible pairs (assumed) ->  29 min
3 000 videos,   ~2.7 M compatible pairs (assumed) -> 4.4 hours
```

Only the per-pair cost and the 276-file pair count are measured; the larger
pair counts assume the same episode-length mix, and the real number depends
entirely on how many files fall inside each other's duration gate.

Every fingerprinting stage uses the worker pool; the pair loop that consumes
their output does not. It is fine for the library it was tuned on and becomes
the dominant cost somewhere around a thousand videos. Worth recording as a
known ceiling now rather than rediscovering it as a Critical later.

### A-25 · Documentation drift has restarted

Version strings are consistent at `1.10.1` (A-09 held), but:

- `docs/STATUS.md:270` — "current package version: `1.9.0`".
- `docs/STATUS.md:233` — "the suite contains 120 tests plus 7 subtests … a
  documented 37-error `mypy` baseline". Actual: 145 tests, 14 subtests, 33
  mypy errors.
- `docs/STATUS.md:282` and `:286` give both "114 s warm" and "105 seconds"
  for the same warm-cache scan.
- `docs/THRESHOLDS.md:27` still parenthesises calibration as applying to
  "image (structure component only — the color component … is left alone)",
  which the section 15 lines below contradicts: colour is no longer part of
  the score at all.

### A-26 · `GRID_MISSING_SAMPLE` is consumed but never produced

`_grid_overlap` and `_shift_score` both skip `-1` placeholders, and
`test_grid_alignment_ignores_missing_samples_without_shifting` pins the
behaviour — but since A-13 made the grid a single decode pass, nothing can
emit a placeholder: ffmpeg's `fps` filter fills gaps by duplication and a
truncated stream simply yields a shorter list. The guard is a leftover from
the seek-based implementation. Harmless, but it and its test describe a
contract the producer no longer has.

### A-27 · The video shift search kept the coarse stride audio deliberately dropped

A-14 removed audio's coarse stride after a random-fingerprint regression test
failed, on the grounds that "that peak can be one item wide" and real
correlation would have hidden the assumption. `_best_grid_shift` still strides
by `GRID_SEARCH_COARSE_STRIDE = 2` and refines ±3 around the winner.

Measured both ways:

```
fully uncorrelated grids, odd true shift:      206/220 mis-aligned
shot-structured grids + re-encode noise:         0/800 mis-aligned
```

So the assumption holds for anything that looks like video, and
`test_grid_alignment_recovers_a_small_offset` already pins a 3-slot (odd)
offset. Recorded for consistency with A-14's reasoning rather than as a defect;
if the stride is kept, the difference from the audio path deserves a comment
saying why.

---

## What is working well

- **The alignment grid (A-01/A-13).** Reproduced end to end on the real
  library from a cold cache — see below. The 2 s cadence and the cached,
  separately versioned grid are the right design, and the cache-invalidation
  plumbing (`GRID_FINGERPRINT_VERSION`, `IMAGE_FINGERPRINT_VERSION`,
  `_ensure_column`) correctly discards pre-1.9.1/1.10.0 fingerprints.
- **The image formula (A-06).** Structure-only with a colour veto reproduces
  its documented behaviour; the veto correctly refuses flat-colour pairs.
- **The dihedral views (A-15).** 22 real posters: rotation and mirroring score
  exactly 100, and border trimming rescues 16/22 letterboxed and 20/22
  margined copies that would otherwise be missed. Within its measured class it
  is an unambiguous win.
- **`samesame-benchmark` coverage (A-16).** The tool runs clean at the shipped
  defaults with >20 points of headroom on every medium, and reading the
  thresholds from `DEFAULT_CONFIG` is the right pattern — A-22 is only that
  one literal escaped it.
- **The source-protection layer.** `actions/`, `consolidation.py` and
  `transcode/` are byte-for-byte unchanged since `55d6e70`, the 1.8.0 audit's
  baseline — `git diff --stat 55d6e70..HEAD -- dedupe/` touches ten files and
  none of them. Re-read this
  time: preflight identity, SHA-256 verification on both sides of every move,
  content-verified rollback, the recycle opt-in, and the "never overwrite an
  existing destination" rule are all intact. No path was found that can delete
  or overwrite a source without an explicit confirmation.
- **Version discipline.** A-09's drift did not come back in the version
  strings themselves; `pyproject.toml`, `dedupe.__version__`, `README.md` and
  all four docs agree on `1.10.1`.
- **The optional AI name provider.** `name_normalizer.MODEL_NAME` pins
  `claude-haiku-4-5`, which is current and not deprecated — the right tier for
  a cheap title-normalisation call, and no migration is owed. The
  `_coerce_normalized_results` path falls back to the local heuristic for any
  title the model omits, so a partial response degrades rather than losing
  files.

## Fix plan

| # | Work | Severity | Status |
| --- | --- | --- | --- |
| A-17 | Bump `SETTINGS_SCHEMA_VERSION`; state the bump rule; test a same-schema stale value | High | **Done in 1.10.2** |
| A-18 | Replace image blocking with a scheme that is selective at a 12-bit radius; stop materialising the pair set | High | Open |
| A-19 | Give audio the video path's sorted sweep; drop the `seen` set | High | Open |
| A-20 | Commit periodically inside long stages; record failed grid builds | High | **Done in 1.10.2** |
| A-21 | Fix `docs/CONFIG.md`'s nested example; make duplicate threshold keys an error | Medium | **Done in 1.10.2** |
| A-22 | Read the audio duration tolerance from the matcher; finish A-08 for images | Medium | Open |
| A-23 | Remove the trim cliff by keeping both descriptors; measure against the negatives | Medium | Open |
| A-24 | Parallelise the video pair loop (record as a known ceiling if deferred) | Low | Open |
| A-25 | Refresh the stale numbers in `STATUS.md` and `THRESHOLDS.md` | Low | Open |
| A-26 | Remove `GRID_MISSING_SAMPLE` and its test, or make the producer emit it | Low | Open |
| A-27 | Document why video keeps the coarse stride audio dropped | Low | Open |
| A-28 | Give Qt windows/dialogs deterministic destruction; find why a stopped player is unsafe to finalize | High | Open (found while fixing A-17) |

## Full-library validation

A scan of the real library at the shipped defaults, from a **completely cold
cache** (fresh sqlite file — durations, proportional fingerprints, alignment
grids and image fingerprints all rebuilt), then an immediate warm rescan.
Every number `docs/STATUS.md` records reproduces **exactly**:

| | STATUS.md | This audit |
| --- | ---: | ---: |
| Files scanned | 298 | **298** |
| Exact duplicate groups | 14 | **14** |
| Video matches | 129 | **129** |
| Image matches | 0 | **0** |
| Audio matches | 0 | **0** |
| Folder pairs | 61 | **61** |
| Name-only hints | 4 | **4** |
| Warm-cache scan | 105 s | **105.5 s** |

```
cold scan, end to end                     2192 s (36.5 min)
  discovery + exact stage (14 groups)       96 s
  video stage (durations + fingerprints
    + grid build + pair loop)              2095 s
  image / audio / name / folder stages       <1 s
warnings logged                                0
warm rescan                                105.5 s, byte-identical results
```

The grid build alone accounted for roughly 28 minutes of the video stage — in
line with the 26 minutes A-13 recorded, and disk-bandwidth-bound as documented
(it reads all 97.5 GB once). **This is the run A-20 is about:** the WAL file
did not grow once for that entire span, and a cancel at any point in it would
have discarded all of it.

The most useful single number is the **lowest reported video match: 28.44**.
A-13 recorded that on this library no pair at all scores between `18.72` and
`28.44`; the shipped `20.0` default therefore sits inside empty space, and
this scan confirms the gap is still empty — 0 of the 129 matches fall below
`28.44`. The clustering is likewise coherent: 90 content clusters, 8 exact and
82 video, and all 61 folder pairs at directory scope (no root-scope
duplicates, so A-11's fix still holds).

Both zeroes remain correct for the reasons the 1.9.4 closing validation gave:
the library's only duplicated poster is byte-identical (reported as an exact
duplicate, deliberately excluded from perceptual matching), and the library is
entirely `.mp4`, which the audio stage is scoped out of by design.

**What this validation cannot show, and did not.** Every High finding above
survives this green run untouched: A-17 lives in `QSettings`, which a CLI scan
never reads; A-18 lives in the blocking branch, which 22 images never reach;
A-19 lives in the audio sweep, which a video-only library skips in 0.3 s; and
A-20 is invisible precisely because nothing was cancelled. The full-library
rule earned its place — it is what caught A-01 and A-02, and it is what proves
here that the matcher itself is sound — but it is a *coverage* instrument, and
this library covers one medium, one scale, and the happy path. Pair it with a
large-image fixture, a standalone-audio fixture, and a cancellation run, or
the next Critical will again be in a branch nothing executed.

## Resolution of A-17, A-20 and A-21 — 1.10.2

Three of the eleven findings this audit opened with are fixed, and a twelfth (A-28) was found while fixing them. Each shipped with a regression test
that was **confirmed to fail against the pre-fix code**, because a test written
after the fact proves nothing unless it is shown to catch the thing it names.

**A-17.** `SETTINGS_SCHEMA_VERSION` is `3`, so a 1.9.0-1.10.1 profile falls back
to current defaults instead of restoring `image=80` / `audio=88`. Two changes
make the omission hard to repeat: the GUI now reads its threshold defaults from
`DEFAULT_CONFIG` instead of repeating them as literals (the drift-killing
pattern A-16 already applied to the benchmark tool), and
`test_shipped_defaults_are_pinned_to_the_settings_schema_version` pins the
shipped defaults *to the schema number*, so changing a default without bumping
it fails the suite. The constant's comment now states the rule as three
triggers — scale, formula, **or default** — since it was the third that was
missed twice.

**A-20.** `Cache.commit_periodically()` commits at most every 30 s; every
fingerprinting stage (exact partial/full hashes, video durations, video
fingerprints, alignment grids, image fingerprints, audio durations and
fingerprints) calls it as it consumes completed futures. The interval is
time-based, not a file count, because per-file cost varies by orders of
magnitude. A failed grid build now stores `GRID_FINGERPRINT_FAILED`
(`-GRID_FINGERPRINT_VERSION`) rather than leaving the column blank, so an
unreadable file is not re-decoded on every scan; negating the version means a
version bump retries past failures, and a changed size or mtime fails
`hydrate_if_current` outright, so a repaired file is retried anyway.

**A-21.** The `docs/CONFIG.md` example says `60`. More usefully,
`_flatten_config` no longer resolves two spellings of one setting by document
order — a config that sets `matching.audio` and `audio.threshold` to
*different* values now exits with both sources named. Agreeing duplicates are
still accepted. A second test parses every JSON block in `docs/CONFIG.md` and
asserts what it actually resolves to, so the prose cannot drift from the
shipped defaults again.

Verified after the fixes:

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **152 passed, 14 subtests** (7 new regressions) |
| Same tests against pre-fix code | **4 of 4 fail** as intended |
| `ruff check .` / `ruff format --check .` | clean |
| `mypy dedupe` | 33 errors — unchanged baseline |
| Full `TestLib` rescan | 100.3 s, **every result section byte-identical** |

The last row is the point: these are durability and configuration fixes, and
detection was expected not to move. It did not — exact groups, video, image and
audio matches, folder pairs, name hints and content clusters all compare equal
to the pre-fix scan.

A-18 and A-19 remain open. Both need a fixture the project does not have yet
(a large image corpus; a standalone-audio library at scale), which is precisely
the gap the validation section above describes.

**A-28 was found by doing this work**, not by reading code: one extra
`MainWindow()` construction, at one specific position in the GUI test order,
turned a latent Qt object-lifetime bug into a process abort in 5 of 6
full-suite runs, in an unrelated test file. It is written up above with the evidence, including the fix that
looked obviously right and made the crash deterministic instead. The 1.10.2
tests were reworked to add no new windows — the reworked A-17 regression
reuses the window the existing schema test already builds, and now pins the
*newest rejected* schema (2) rather than the oldest, so it tests the fix that
actually shipped. The suite runs 10 of 10 clean.

## The recurring lesson, one level further out

The 1.8.0 audit closed with: *a fast green suite says nothing about the code
paths it never executes*. Three of this audit's four High findings are a
different sentence:

**A constant that guards an invariant has to be re-derived whenever the
invariant moves.** `ALIGNMENT_RECHECK_FLOOR` (A-01) stopped filtering when the
noise floor was understood; `SETTINGS_SCHEMA_VERSION` (A-17) stopped
protecting when the thresholds were redefined; the LSH block width (A-18)
stopped selecting when `image_threshold` fell; the audio bucket window (A-19)
stopped being cheap when the tolerance widened. In every case the constant was
correct when written, the change that invalidated it was itself correct and
well-measured, and nothing connected the two.

The cheap countermeasure is a test that asserts the *relationship* rather than
the value: that blocking rejects most unrelated pairs at whatever
`image_threshold` currently is; that the audio sweep compares O(n) pairs per
file at whatever tolerance is currently set; that a stored setting written by
the previous release still means what it says.
