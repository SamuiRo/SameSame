# FFmpeg Presets for Films and General Video

## Status

Added in `1.17.0` alongside the four anime presets in
`ANIME_ENCODING_PRESETS.md`, which are unchanged. These are the source
specification for the `film_*` and `video_*` preset IDs. The queue, safety,
stream copying, and validation rules are identical for every preset: audio,
subtitles, chapters, metadata, and attachments are copied, the source is kept,
and an output that is not smaller than its source is skipped.

Every argument set below was initialized and run end to end through
`samesame-transcode` (encode, probe, full decode validation) with FFmpeg 2026-06
and an RTX 5070 Ti. Speed and size depend heavily on the source, so no size
estimates are given: **test on one file first**.

## Why live action needs different settings

Anime is flat colour and line art, so its presets protect sharp edges
(`deblock=-1,-1`) and dark flat areas (`aq-mode=3`). Live action is mostly
texture, grain, and gradients (skies, skin, dark scenes). The encoder's
temptation there is to smooth texture into blur, which looks "clean" but
plastic. The film presets therefore:

- raise psychovisual weighting (`psy-rd=2.0:psy-rdoq=1.5`) so x265 keeps
  texture instead of blurring it;
- keep SAO off, which would otherwise smear fine detail;
- use a slightly gentler AQ (`aq-strength=0.9`);
- stay **10-bit on every encoder, NVENC included** (`p010le`), because
  gradients band far more visibly in live action and 10-bit/HDR sources must
  not be truncated to 8 bits;
- enable spatial and temporal AQ plus lookahead on NVENC, which the anime
  NVENC presets leave off.

## Film presets (movies, live-action series)

| Preset ID | Encoder | Key settings | Use when |
| --- | --- | --- | --- |
| `film_x265_max` | `libx265` | CRF 20, `slower`, film params, 10-bit | Archive copy, time is no issue |
| `film_x265_balanced` | `libx265` | CRF 22, `slow`, film params, 10-bit | **Default choice for films** |
| `film_x265_grain` | `libx265` | CRF 20, `slow`, `-tune grain`, 10-bit | Old or heavily grainy 35mm transfers where grain must survive; saves the least |
| `film_svtav1` | `libsvtav1` | CRF 28, preset 4, `tune=0:film-grain=8:film-grain-denoise=0`, 10-bit | Smallest files on CPU; grain is re-synthesized on playback |
| `film_av1_nvenc` | `av1_nvenc` | CQ 26, `p7`, `hq`, multipass fullres, lookahead 32, spatial+temporal AQ, `p010le` | Fast, good quality; RTX 40+ only |
| `film_hevc_nvenc` | `hevc_nvenc` | CQ 24, `p7`, `hq`, multipass fullres, lookahead 32, spatial+temporal AQ, `main10`, `p010le` | Fast on any NVENC GPU, plays everywhere HEVC does |

`film_x265_*` share these x265 parameters:

```
aq-mode=3:aq-strength=0.9:psy-rd=2.0:psy-rdoq=1.5:no-sao=1:deblock=-1,-1
```

## General video presets (phone, camera, screen recordings, web video)

These favour size and speed over archival fidelity.

| Preset ID | Encoder | Key settings |
| --- | --- | --- |
| `video_x265_compact` | `libx265` | CRF 24, `medium`, `aq-mode=3`, 10-bit |
| `video_hevc_nvenc_fast` | `hevc_nvenc` | CQ 27, `p5`, `hq`, lookahead 20, spatial+temporal AQ, `main10`, `p010le` |

## Notes

- **CRF/CQ scales differ per encoder.** SVT-AV1 CRF 28 is not comparable to
  x265 CRF 28; NVENC uses `-cq`, not `-crf`.
- **Grain synthesis** (`film_svtav1`): the encoder measures the source grain,
  encodes a cleaner picture, and stores a grain model that the decoder adds
  back. Any AV1 decoder applies it; a few hardware players ignore it and show
  a slightly cleaner image.
- **HDR**: all film presets are 10-bit and FFmpeg carries the colour tags, but
  Dolby Vision metadata is not preserved by any preset. Keep Dolby Vision
  sources as they are.
- **Already-efficient sources** (HEVC/AV1 web releases) often cannot be made
  meaningfully smaller; SameSame skips the output rather than keep a larger
  file.
- **Hardware presets** need a capability check to pass first. Before `1.17.0`
  that check used a 64x64 test frame, which current NVENC rejects as below its
  minimum size, so every NVENC preset was wrongly reported unavailable. It now
  uses 256x256.
