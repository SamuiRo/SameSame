from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranscodePreset:
    preset_id: str
    name: str
    encoder: str
    video_args: tuple[str, ...]
    hardware: bool = False
    description: str = ""
    #: Content family the preset is tuned for: "anime", "film" or "video".
    #: Request-scoped custom presets are "custom".
    category: str = "custom"


#: x265 tuning for live action. Unlike anime's flat colours and line art, film
#: is mostly texture and grain: stronger psychovisual RD keeps that texture
#: instead of smoothing it into blur, SAO stays off for the same reason, and a
#: slightly gentler AQ avoids starving bright areas to feed the dark ones.
_FILM_X265_PARAMS = "aq-mode=3:aq-strength=0.9:psy-rd=2.0:psy-rdoq=1.5:no-sao=1:deblock=-1,-1"


PRESETS: dict[str, TranscodePreset] = {
    "anime_x265_max": TranscodePreset(
        preset_id="anime_x265_max",
        name="Anime x265 maximum quality",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "20",
            "-preset",
            "slower",
            "-x265-params",
            "no-sao=1:aq-mode=3:deblock=-1,-1",
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 20 · slower · 10-bit",
        category="anime",
    ),
    "anime_x265_balanced": TranscodePreset(
        preset_id="anime_x265_balanced",
        name="Anime x265 balanced",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "22",
            "-preset",
            "slow",
            "-x265-params",
            "no-sao=1:aq-mode=3:deblock=-1,-1",
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 22 · slow · 10-bit",
        category="anime",
    ),
    "anime_av1_nvenc": TranscodePreset(
        preset_id="anime_av1_nvenc",
        name="Anime NVIDIA AV1",
        encoder="av1_nvenc",
        video_args=(
            "-c:v",
            "av1_nvenc",
            "-cq",
            "22",
            "-preset",
            "p7",
            "-tune",
            "hq",
            "-multipass",
            "fullres",
            "-pix_fmt",
            "yuv420p",
        ),
        hardware=True,
        description="NVIDIA RTX 40+ · CQ 22 · p7",
        category="anime",
    ),
    "anime_hevc_nvenc": TranscodePreset(
        preset_id="anime_hevc_nvenc",
        name="Anime NVIDIA HEVC",
        encoder="hevc_nvenc",
        video_args=(
            "-c:v",
            "hevc_nvenc",
            "-cq",
            "22",
            "-preset",
            "p7",
            "-tune",
            "hq",
            "-pix_fmt",
            "yuv420p",
        ),
        hardware=True,
        description="NVIDIA · CQ 22 · p7",
        category="anime",
    ),
    "film_x265_max": TranscodePreset(
        preset_id="film_x265_max",
        name="Film x265 maximum quality",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "20",
            "-preset",
            "slower",
            "-x265-params",
            _FILM_X265_PARAMS,
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 20 · slower · 10-bit",
        category="film",
    ),
    "film_x265_balanced": TranscodePreset(
        preset_id="film_x265_balanced",
        name="Film x265 balanced",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "22",
            "-preset",
            "slow",
            "-x265-params",
            _FILM_X265_PARAMS,
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 22 · slow · 10-bit",
        category="film",
    ),
    "film_x265_grain": TranscodePreset(
        preset_id="film_x265_grain",
        name="Film x265 heavy grain",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "20",
            "-preset",
            "slow",
            "-tune",
            "grain",
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 20 · slow · tune grain · 10-bit · old/grainy film, smaller savings",
        category="film",
    ),
    "film_svtav1": TranscodePreset(
        preset_id="film_svtav1",
        name="Film SVT-AV1 compact",
        encoder="libsvtav1",
        video_args=(
            "-c:v",
            "libsvtav1",
            "-crf",
            "28",
            "-preset",
            "4",
            "-svtav1-params",
            "tune=0:film-grain=8:film-grain-denoise=0",
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · AV1 CRF 28 · preset 4 · grain synthesis · 10-bit",
        category="film",
    ),
    "film_av1_nvenc": TranscodePreset(
        preset_id="film_av1_nvenc",
        name="Film NVIDIA AV1",
        encoder="av1_nvenc",
        video_args=(
            "-c:v",
            "av1_nvenc",
            "-cq",
            "26",
            "-preset",
            "p7",
            "-tune",
            "hq",
            "-multipass",
            "fullres",
            "-rc-lookahead",
            "32",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-pix_fmt",
            "p010le",
        ),
        hardware=True,
        description="NVIDIA RTX 40+ · CQ 26 · p7 · AQ · 10-bit",
        category="film",
    ),
    "film_hevc_nvenc": TranscodePreset(
        preset_id="film_hevc_nvenc",
        name="Film NVIDIA HEVC",
        encoder="hevc_nvenc",
        video_args=(
            "-c:v",
            "hevc_nvenc",
            "-cq",
            "24",
            "-preset",
            "p7",
            "-tune",
            "hq",
            "-multipass",
            "fullres",
            "-rc-lookahead",
            "32",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-profile:v",
            "main10",
            "-pix_fmt",
            "p010le",
        ),
        hardware=True,
        description="NVIDIA · CQ 24 · p7 · AQ · 10-bit",
        category="film",
    ),
    "video_x265_compact": TranscodePreset(
        preset_id="video_x265_compact",
        name="Video x265 compact",
        encoder="libx265",
        video_args=(
            "-c:v",
            "libx265",
            "-crf",
            "24",
            "-preset",
            "medium",
            "-x265-params",
            "aq-mode=3",
            "-pix_fmt",
            "yuv420p10le",
        ),
        description="CPU · CRF 24 · medium · 10-bit · phone, camera, screen, web video",
        category="video",
    ),
    "video_hevc_nvenc_fast": TranscodePreset(
        preset_id="video_hevc_nvenc_fast",
        name="Video NVIDIA HEVC fast",
        encoder="hevc_nvenc",
        video_args=(
            "-c:v",
            "hevc_nvenc",
            "-cq",
            "27",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc-lookahead",
            "20",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-profile:v",
            "main10",
            "-pix_fmt",
            "p010le",
        ),
        hardware=True,
        description="NVIDIA · CQ 27 · p5 · AQ · 10-bit · large batches of everyday video",
        category="video",
    ),
}


def get_preset(preset_id: str) -> TranscodePreset:
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        choices = ", ".join(PRESETS)
        raise ValueError(f"Unknown preset '{preset_id}'. Available presets: {choices}") from exc
