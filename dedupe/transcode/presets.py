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
    ),
}


def get_preset(preset_id: str) -> TranscodePreset:
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        choices = ", ".join(PRESETS)
        raise ValueError(f"Unknown preset '{preset_id}'. Available presets: {choices}") from exc
