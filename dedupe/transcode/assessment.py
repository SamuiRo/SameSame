"""How raw a video is, and whether re-encoding it is likely to pay off.

The estimate compares the file's video bitrate with what an efficient modern
encode of the same resolution and frame rate typically needs. It is a triage
aid for picking files, not a promise: actual results depend on the content.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Video bitrate (bits/s) an efficient AV1 encode typically needs at 1920×1080
#: and ~24 fps, per content family. Anime's flat colour needs the least; film
#: grain and gameplay motion need more.
REFERENCE_1080P_BPS = {
    "anime": 2.2e6,
    "film": 3.2e6,
    "game": 3.5e6,
    "video": 3.0e6,
    "custom": 3.0e6,
}

#: How many more bits each output encoder needs than SVT-AV1 for the same look.
ENCODER_FACTOR = {
    "libsvtav1": 1.0,
    "av1_nvenc": 1.15,
    "libx265": 1.25,
    "hevc_nvenc": 1.45,
}

#: How many more bits each *source* codec needs than AV1 for the same look. A
#: source well above its own codec's efficient level is "raw"; one near it was
#: already encoded sensibly, so re-encoding mostly trades quality for space.
SOURCE_CODEC_FACTOR = {
    "av1": 1.0,
    "hevc": 1.4,
    "vp9": 1.4,
    "h264": 2.0,
    "vc1": 2.5,
    "mpeg4": 2.5,
    "msmpeg4v3": 2.5,
    "mpeg2video": 3.0,
    "mpeg1video": 3.5,
}
_DEFAULT_SOURCE_FACTOR = 2.0

#: Audio is copied unchanged by every preset, so its bytes stay in the output.
#: Used when the container reports no per-stream bitrate.
_LOSSLESS_AUDIO = {"flac", "truehd", "mlp", "alac", "dts", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray"}
_LOSSLESS_AUDIO_FALLBACK_BPS = 900e3
_LOSSY_AUDIO_FALLBACK_BPS = 160e3

_REFERENCE_PIXELS = 1920 * 1080


class Verdict(str, Enum):
    WORTH = "worth"
    MAYBE = "maybe"
    SKIP = "skip"

    @property
    def label(self) -> str:
        return {"worth": "Worth it", "maybe": "Maybe", "skip": "Skip"}[self.value]

    @property
    def rank(self) -> int:
        return {"worth": 2, "maybe": 1, "skip": 0}[self.value]


@dataclass(frozen=True, slots=True)
class AudioTrack:
    codec: str
    bit_rate: int | None = None

    @property
    def lossless(self) -> bool:
        return self.codec.startswith("pcm_") or self.codec in _LOSSLESS_AUDIO

    @property
    def assumed_bps(self) -> float:
        if self.bit_rate:
            return float(self.bit_rate)
        return _LOSSLESS_AUDIO_FALLBACK_BPS if self.lossless else _LOSSY_AUDIO_FALLBACK_BPS


@dataclass(frozen=True, slots=True)
class CompressionAssessment:
    verdict: Verdict
    #: Video bitrate relative to an efficient encode in the source's own codec:
    #: ~1 means sensibly encoded already, 3+ means close to a disc rip.
    rawness: float
    total_bps: float
    video_bps: float
    target_video_bps: float
    estimated_output_size: int
    estimated_saving: int
    saving_fraction: float
    lossless_audio_bps: float

    @property
    def rawness_label(self) -> str:
        if self.rawness >= 3.0:
            return "Raw"
        if self.rawness >= 1.6:
            return "Heavy"
        if self.rawness >= 1.15:
            return "Moderate"
        return "Compact"


def assess_compression(
    *,
    size: int,
    duration: float,
    codec: str,
    width: int | None,
    height: int | None,
    frame_rate: float | None = None,
    audio: tuple[AudioTrack, ...] = (),
    category: str = "anime",
    encoder: str = "libsvtav1",
) -> CompressionAssessment | None:
    """Estimate what re-encoding this file would save; None when unknowable."""
    if size <= 0 or duration <= 0 or not height:
        return None
    pixels = (width or round(height * 16 / 9)) * height
    fps_factor = 1.0
    if frame_rate and frame_rate > 0:
        # Bits grow slower than frames: consecutive frames predict each other.
        fps_factor = min(1.6, max(0.8, (frame_rate / 24.0) ** 0.5))
    reference = REFERENCE_1080P_BPS.get(category, REFERENCE_1080P_BPS["custom"])
    efficient_av1_bps = reference * (pixels / _REFERENCE_PIXELS) ** 0.75 * fps_factor
    target_video_bps = efficient_av1_bps * ENCODER_FACTOR.get(encoder, 1.25)

    total_bps = size * 8 / duration
    audio_bps = sum(track.assumed_bps for track in audio)
    # Never let a guessed audio bitrate eat the whole file.
    video_bps = max(total_bps - audio_bps, total_bps * 0.5)
    lossless_audio_bps = sum(track.assumed_bps for track in audio if track.lossless)

    saving = max(0.0, (video_bps - target_video_bps) * duration / 8)
    saving = min(saving, size * 0.95)
    saving_fraction = saving / size
    source_factor = SOURCE_CODEC_FACTOR.get(codec.casefold(), _DEFAULT_SOURCE_FACTOR)
    rawness = video_bps / (efficient_av1_bps * source_factor)

    if saving_fraction < 0.15:
        verdict = Verdict.SKIP
    elif saving_fraction >= 0.6 or (saving_fraction >= 0.35 and rawness >= 1.3):
        verdict = Verdict.WORTH
    else:
        # Space can be won, but mostly by re-compressing an already sensible
        # encode, where generation loss shows first in gradients and dark scenes.
        verdict = Verdict.MAYBE

    return CompressionAssessment(
        verdict=verdict,
        rawness=rawness,
        total_bps=total_bps,
        video_bps=video_bps,
        target_video_bps=target_video_bps,
        estimated_output_size=max(0, round(size - saving)),
        estimated_saving=round(saving),
        saving_fraction=saving_fraction,
        lossless_audio_bps=lossless_audio_bps,
    )
