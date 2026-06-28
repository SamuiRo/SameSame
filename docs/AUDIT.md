# Project Audit

Audit date: 2026-06-28  
Audited version: `1.6.4`

## Outcome

The scanner, reports, cache, desktop review workflow, journaled file actions,
batch compression, transcoding validation, promotion flow, command-line tools,
and package metadata were reviewed. The complete automated suite passes after
the corrections below. No unresolved issue was found that permits an
unconfirmed permanent deletion or source overwrite.

## Corrected Findings

### High: failed quarantine verification could strand a moved file

Quarantine and restore verified content after moving it. If that verification
raised an I/O or identity error, the operation was journaled as failed but the
file could remain at the destination without a reversible completed record.

Both directions now attempt an immediate content-verified rollback. The journal
message records whether rollback succeeded, was impossible, or also failed.
Regression tests cover failed quarantine verification and failed restore
verification.

### High: exact-group batch cleanup relied on a non-cryptographic match hash

Exact discovery uses full-file XXH3-128 for speed. That is appropriate for
grouping, but a destructive batch should not depend on collision resistance
alone. Before each "keep one, quarantine the other copies" operation, SameSame
now computes SHA-256 for both the selected keeper and candidate. A mismatch or
changed file skips the candidate without moving it.

### Medium: unsafe CLI numeric values were accepted

Negative, non-finite, or greater-than-100 thresholds were accepted, and an
arbitrarily large worker count could request excessive threads. All similarity
thresholds must now be finite values from `0` through `100`; workers must be
from `1` through `64`. Config-file name providers and log levels are also
validated before scanning starts.

### Medium: package version metadata was inconsistent

`pyproject.toml`, `dedupe.__version__`, and the current documentation reported
different versions. They now agree on `1.6.4`, with a regression test comparing
the package constant to project metadata.

### Low: developer installs did not request the declared build backend

The project requires `setuptools>=77`, but the development extra did not include
it. A stale editable environment could therefore fail a non-isolated build and
miss newly added console entry points. The development extra now includes the
same backend requirement. A fresh isolated build and clean wheel installation
were verified.

### High: preview handles blocked removal and OS recycle could be permanent

Qt Multimedia could retain the selected video handle while a background action
attempted to move it, producing Windows sharing violation error 32. SameSame now
stops and detaches both preview players before starting file mutations, yields
one event-loop turn, and retries only transient Windows sharing violations.

OS Recycle is now blocked by default. Recycle buttons offer reversible SameSame
Quarantine instead, and post-encode cleanup also defaults to quarantine. Calling
the operating-system recycle integration requires a separate red unsafe opt-in.
This is necessary because Microsoft documents that Recycle Bin Properties
control whether files are stored or deleted immediately:
<https://learn.microsoft.com/en-us/previous-versions/ff486319(v=vs.85)>.

## Verification Matrix

- `python -m unittest discover -s tests`: all tests pass, including real FFmpeg
  video, audio, x265, GUI-thread, promotion, recycle, and rollback coverage.
- `python -m pytest -q`: the same complete suite passes.
- `ruff check .`: no findings.
- `python -m compileall -q dedupe tests`: successful.
- `git diff --check`: successful.
- `pip check`: no broken installed dependencies.
- every JSON file under `docs/`: parses successfully.
- `samesame --help`, `samesame-benchmark --help`, transcode preset listing, and
  offscreen GUI construction: successful.
- isolated PEP 517 build: produced `samesame-1.6.4.tar.gz` and
  `samesame-1.6.4-py3-none-any.whl`.
- clean temporary wheel installation: package reports `1.6.4` and installs the
  `samesame-transcode` entry point.

## Remaining Known Risks and Limits

- Similar-image, video, and audio results are probabilistic review evidence,
  not proof of identical editions. Only exact-group batch cleanup adds keeper
  equality verification.
- Unsafe Recycle Bin behavior belongs to the operating system and is not
  automatically reversible through SameSame. It is blocked by default;
  quarantine remains the safe path.
- Transcode validation checks container readability, duration, stream/chapter
  preservation, size, and first-frame decoding. It is not a full-file decode,
  perceptual quality measurement, or VMAF analysis.
- NVENC presets remain dependent on the installed FFmpeg build, NVIDIA driver,
  and compatible hardware. Capability checks reduce but cannot eliminate
  runtime driver failures.
- The duplicate scanner does not intentionally traverse directory symlinks or
  junctions. The dedicated compression-folder loader does, with cycle
  detection. Add every real collection root explicitly when scanning duplicates.
- SQLite cache and journal files are designed for one active SameSame process;
  simultaneous writers from multiple application instances are not a supported
  workflow.
- Cache rows for files no longer present are retained. They do not participate
  in current scans, but the database can grow until manually replaced.
- The suite has broad functional and integration coverage, but no percentage
  coverage gate is configured.
- `ruff check` is clean; `ruff format --check` still reports pre-existing style
  drift in multiple files. Formatting the whole repository should be a separate
  mechanical change to avoid mixing it with behavior changes.

## Environment Note

The existing workspace `.venv` was created before the transcode console entry
point and still lacks `samesame-transcode.exe`. The built wheel contains and
successfully installs that entry point. Refresh the editable environment with:

```powershell
python -m pip install -e ".[gui,dev]"
```
