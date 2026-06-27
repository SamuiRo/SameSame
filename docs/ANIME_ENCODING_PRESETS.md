# FFmpeg Presets for Anime Compression

## Status

This document is the source specification for SameSame's transcoding module,
implemented in `1.5.5` and exposed through `samesame-transcode`. Safety
requirements and the remaining desktop integration work are tracked in
`ROADMAP.md`.

The arguments below are the four initial built-in presets. Reported speed and
file-size figures are reference estimates for a roughly 25-minute 720p anime
episode, not guaranteed outputs. Source complexity, grain, resolution, frame
rate, audio, GPU, and FFmpeg version can change the result substantially.

## 🏆 PRESET 1 — libx265 · Maximum Quality (recommended)

**When to use:** time is not an issue, you want the best possible result, permanent archive.

| Parameter | Value |
|-----------|-------|
| Codec | `libx265` (CPU) |
| CRF | `20` |
| Preset | `slower` |
| Speed | 🐢 Slow (~15–40 min / episode) |
| File size | ~150–250 MB / 720p episode |
| Quality | ⭐⭐⭐⭐⭐ |

```python
ENCODER = "libx265"
CRF     = 20
PRESET  = "slower"

video_args = [
    "-c:v", "libx265",
    "-crf", "20",
    "-preset", "slower",
    "-x265-params", "no-sao=1:aq-mode=3:deblock=-1,-1",
    "-pix_fmt", "yuv420p10le",   # 10-bit — less banding in anime
]
```

**Parameter breakdown:**
- `no-sao=1` — disables the edge-blurring that H.265 applies by default
- `aq-mode=3` — better detail retention in dark and flat-color areas
- `deblock=-1,-1` — sharper edges (important for anime line art)
- `yuv420p10le` — 10-bit color, noticeably fewer gradient artifacts

---

## ⚡ PRESET 2 — libx265 · Quality / Speed Balance

**When to use:** want libx265 quality but don't want to wait as long.

| Parameter | Value |
|-----------|-------|
| Codec | `libx265` (CPU) |
| CRF | `22` |
| Preset | `slow` |
| Speed | 🐌 Slow (~10–25 min / episode) |
| File size | ~100–180 MB / 720p episode |
| Quality | ⭐⭐⭐⭐½ |

```python
ENCODER = "libx265"
CRF     = 22
PRESET  = "slow"

video_args = [
    "-c:v", "libx265",
    "-crf", "22",
    "-preset", "slow",
    "-x265-params", "no-sao=1:aq-mode=3:deblock=-1,-1",
    "-pix_fmt", "yuv420p10le",
]
```

---

## 🚀 PRESET 3 — av1_nvenc · GPU AV1 (quality + speed)

**When to use:** want good quality but 5–8× faster than CPU.
RTX 40/50 series — the AV1 hardware encoder is very capable.

| Parameter | Value |
|-----------|-------|
| Codec | `av1_nvenc` (GPU) |
| CQ | `22` |
| Preset | `p7` |
| Speed | ⚡ Fast (~3–8 min / episode) |
| File size | ~120–200 MB / 720p episode |
| Quality | ⭐⭐⭐⭐ |

```python
ENCODER = "av1_nvenc"
CQ      = 22

video_args = [
    "-c:v", "av1_nvenc",
    "-cq", "22",             # CQ instead of CRF for nvenc
    "-preset", "p7",         # p1 (fastest) → p7 (best quality)
    "-tune", "hq",
    "-multipass", "fullres", # two-pass encode → better quality
    "-pix_fmt", "yuv420p",
]
```

> ⚠️ `av1_nvenc` requires RTX 40+ series. On RTX 30 — use `hevc_nvenc` instead.

---

## 🎮 PRESET 4 — hevc_nvenc · GPU H.265 (maximum speed)

**When to use:** need to process a large batch quickly, quality is less critical.

| Parameter | Value |
|-----------|-------|
| Codec | `hevc_nvenc` (GPU) |
| CQ | `22` |
| Preset | `p7` |
| Speed | ⚡⚡ Very fast (~1–3 min / episode) |
| File size | ~150–250 MB / 720p episode |
| Quality | ⭐⭐⭐½ |

```python
ENCODER = "hevc_nvenc"
CQ      = 22

video_args = [
    "-c:v", "hevc_nvenc",
    "-cq", "22",
    "-preset", "p7",
    "-tune", "hq",
    "-pix_fmt", "yuv420p",
]
```

---

## 📊 Comparison Table

| Preset | Codec | Quality | File size | Speed |
|--------|-------|---------|-----------|-------|
| #1 Max quality | libx265 CRF 20 slower | ⭐⭐⭐⭐⭐ | ~200 MB | 🐢 ~30 min |
| #2 Balanced | libx265 CRF 22 slow | ⭐⭐⭐⭐½ | ~140 MB | 🐌 ~18 min |
| #3 GPU AV1 | av1_nvenc CQ 22 p7 | ⭐⭐⭐⭐ | ~160 MB | ⚡ ~5 min |
| #4 GPU fast | hevc_nvenc CQ 22 p7 | ⭐⭐⭐½ | ~200 MB | ⚡⚡ ~2 min |

> All figures are approximate for a 720p anime episode of ~25 minutes.

---

## 🔧 Planned SameSame Preset IDs

The preset registry exposes stable IDs rather than requiring users to edit
Python code:

| Preset ID | Encoder settings |
| --- | --- |
| `anime_x265_max` | `libx265`, CRF 20, `slower` |
| `anime_x265_balanced` | `libx265`, CRF 22, `slow` |
| `anime_av1_nvenc` | `av1_nvenc`, CQ 22, `p7` |
| `anime_hevc_nvenc` | `hevc_nvenc`, CQ 22, `p7` |

The command builder translates these IDs into the exact arguments specified
above. It first verifies that the selected encoder exists in the installed
FFmpeg build and that hardware initialization succeeds.

Implementation defaults:

- output container: MKV;
- copy audio and subtitles without re-encoding;
- preserve chapters, metadata, and MKV attachments where possible;
- write to a temporary output and validate it before offering any source-file
  action;
- keep the original unless the user separately confirms quarantine or recycle.

---

## 💡 Tips

- **Always test on a single episode** before running a full batch
- **Check the output visually** — dark scenes, fast motion, and gradients are where artifacts show first
- **10-bit:** the two initial libx265 presets use `yuv420p10le`; the initial
  NVENC presets intentionally use `yuv420p`
- **CQ vs CRF:** nvenc encoders use `-cq`, not `-crf` — these are different parameters, easy to mix up!
- **Audio and subtitles** — always copy without re-encoding (`-c:a copy -c:s copy`)
