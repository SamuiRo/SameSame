from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from dedupe.audio_fingerprint import audio_similarity, fingerprint_audio
from dedupe.image_fingerprint import fingerprint_image, image_similarity
from dedupe.models import FileRecord
from dedupe.thresholds import LabeledScore, summarize_scores
from dedupe.video_fingerprint import (
    check_video_tools,
    fingerprint_video,
    get_duration,
    video_durations_compatible,
    video_similarity,
)


@dataclass(frozen=True, slots=True)
class PairSpec:
    media_type: str
    name: str
    left: Path
    right: Path
    expected_match: bool


def _record(path: Path) -> FileRecord:
    stat = path.stat()
    return FileRecord(path.resolve(), path.parent.resolve(), stat.st_size, stat.st_mtime, path.stem)


def _run_ffmpeg(ffmpeg: str, *arguments: str) -> None:
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", *arguments],
        capture_output=True,
        check=True,
        timeout=120,
    )


def _generate_images(root: Path) -> list[PairSpec]:
    root.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    pairs: list[PairSpec] = []
    palettes = [
        ("navy", "gold", "crimson"),
        ("darkgreen", "cyan", "orange"),
        ("purple", "lime", "white"),
    ]
    for index, (background, primary, secondary) in enumerate(palettes):
        source = root / f"family-{index}-source.png"
        resized = root / f"family-{index}-resized.jpg"
        adjusted = root / f"family-{index}-adjusted.webp"
        image = Image.new("RGB", (320, 200), background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((25 + index * 8, 20, 295, 180 - index * 7), fill=primary)
        draw.ellipse((85, 35 + index * 5, 235, 175), fill=secondary)
        draw.line((0, 199 - index * 15, 319, index * 20), fill="white", width=7)
        image.save(source)
        image.resize((800, 500), Image.Resampling.LANCZOS).save(resized, quality=58)
        ImageEnhance.Brightness(image.resize((480, 300))).enhance(0.78).save(adjusted, quality=70)
        sources.append(source)
        pairs.extend(
            [
                PairSpec("image", f"image-{index}-resize-jpeg", source, resized, True),
                PairSpec("image", f"image-{index}-brightness-webp", source, adjusted, True),
            ]
        )
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            pairs.append(PairSpec("image", f"image-negative-{left.stem}-{right.stem}", left, right, False))
    return pairs


def _generate_videos(root: Path, ffmpeg: str) -> list[PairSpec]:
    root.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    pairs: list[PairSpec] = []
    generators = [
        "testsrc2=size=320x180:rate=12:duration=6",
        "smptebars=size=320x180:rate=12:duration=6",
        "rgbtestsrc=size=320x180:rate=12:duration=6",
    ]
    for index, generator in enumerate(generators):
        source = root / f"family-{index}-source.mp4"
        resized = root / f"family-{index}-resized.avi"
        extended = root / f"family-{index}-extended.mkv"
        _run_ffmpeg(
            ffmpeg,
            "-f",
            "lavfi",
            "-i",
            generator,
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
        _run_ffmpeg(
            ffmpeg,
            "-i",
            str(source),
            "-vf",
            "scale=240:136",
            "-c:v",
            "mpeg4",
            "-q:v",
            "12",
            str(resized),
        )
        _run_ffmpeg(
            ffmpeg,
            "-i",
            str(source),
            "-vf",
            "tpad=stop_mode=clone:stop_duration=1",
            "-c:v",
            "mpeg4",
            "-q:v",
            "8",
            str(extended),
        )
        sources.append(source)
        pairs.extend(
            [
                PairSpec("video", f"video-{index}-resize-reencode", source, resized, True),
                PairSpec("video", f"video-{index}-duration-plus-one", source, extended, True),
            ]
        )
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            pairs.append(PairSpec("video", f"video-negative-{left.stem}-{right.stem}", left, right, False))
    return pairs


def _generate_audio(root: Path, ffmpeg: str) -> list[PairSpec]:
    root.mkdir(parents=True, exist_ok=True)
    sources: list[Path] = []
    pairs: list[PairSpec] = []
    frequencies = [(330, 550), (440, 770), (880, 1320)]
    for index, (left_frequency, right_frequency) in enumerate(frequencies):
        source = root / f"family-{index}-source.wav"
        mp3 = root / f"family-{index}-96k.mp3"
        flac = root / f"family-{index}-quiet.flac"
        _run_ffmpeg(
            ffmpeg,
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={left_frequency}:sample_rate=44100:duration=20",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={right_frequency}:sample_rate=44100:duration=20",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:weights=1 0.45,volume=0.7",
            "-c:a",
            "pcm_s16le",
            str(source),
        )
        _run_ffmpeg(ffmpeg, "-i", str(source), "-c:a", "libmp3lame", "-b:a", "96k", str(mp3))
        _run_ffmpeg(ffmpeg, "-i", str(source), "-af", "volume=0.55", "-c:a", "flac", str(flac))
        sources.append(source)
        pairs.extend(
            [
                PairSpec("audio", f"audio-{index}-mp3", source, mp3, True),
                PairSpec("audio", f"audio-{index}-quiet-flac", source, flac, True),
            ]
        )
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            pairs.append(PairSpec("audio", f"audio-negative-{left.stem}-{right.stem}", left, right, False))
    return pairs


def _load_manifest(path: Path) -> list[PairSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    return [
        PairSpec(
            media_type=str(item["media_type"]),
            name=str(item["name"]),
            left=(base / item["left"]).resolve(),
            right=(base / item["right"]).resolve(),
            expected_match=bool(item["expected_match"]),
        )
        for item in data["pairs"]
    ]


def _evaluate_pairs(pairs: list[PairSpec], ffmpeg: str, ffprobe: str) -> dict[str, list[LabeledScore]]:
    image_cache: dict[Path, list[int]] = {}
    video_cache: dict[Path, FileRecord] = {}
    audio_cache: dict[Path, list[int]] = {}
    audio_duration_cache: dict[Path, float] = {}
    results: dict[str, list[LabeledScore]] = {"image": [], "video": [], "audio": []}

    for pair in pairs:
        note = None
        eligible = True
        if pair.media_type == "image":
            for path in (pair.left, pair.right):
                if path not in image_cache:
                    fingerprint = fingerprint_image(_record(path))
                    if fingerprint is None:
                        raise RuntimeError(f"Cannot fingerprint image: {path}")
                    image_cache[path] = fingerprint
            similarity = image_similarity(image_cache[pair.left], image_cache[pair.right])
            raw_similarity = similarity
        elif pair.media_type == "video":
            for path in (pair.left, pair.right):
                if path not in video_cache:
                    record = _record(path)
                    record.duration = get_duration(path, ffprobe)
                    if record.duration is None:
                        raise RuntimeError(f"Cannot read video duration: {path}")
                    record.fingerprint = fingerprint_video(path, record.duration, ffmpeg)
                    if record.fingerprint is None:
                        raise RuntimeError(f"Cannot fingerprint video: {path}")
                    video_cache[path] = record
            duration_delta = abs(
                (video_cache[pair.left].duration or 0.0) - (video_cache[pair.right].duration or 0.0)
            )
            raw_similarity = video_similarity(video_cache[pair.left], video_cache[pair.right], ffmpeg)
            if not video_durations_compatible(
                video_cache[pair.left].duration or 0.0,
                video_cache[pair.right].duration or 0.0,
            ):
                similarity = 0.0
                eligible = False
                note = (
                    f"duration delta {duration_delta:.3f}s exceeds matcher ratio/delta limits"
                )
            else:
                similarity = raw_similarity
        elif pair.media_type == "audio":
            for path in (pair.left, pair.right):
                if path not in audio_cache:
                    fingerprint = fingerprint_audio(_record(path), ffmpeg)
                    if fingerprint is None:
                        raise RuntimeError(f"Cannot fingerprint audio: {path}")
                    audio_cache[path] = fingerprint
                    duration = get_duration(path, ffprobe)
                    if duration is None:
                        raise RuntimeError(f"Cannot read audio duration: {path}")
                    audio_duration_cache[path] = duration
            duration_delta = abs(audio_duration_cache[pair.left] - audio_duration_cache[pair.right])
            raw_similarity = audio_similarity(audio_cache[pair.left], audio_cache[pair.right])
            if duration_delta > 3.0:
                similarity = 0.0
                eligible = False
                note = f"duration delta {duration_delta:.3f}s exceeds matcher tolerance 3.0s"
            else:
                similarity = raw_similarity
        else:
            raise ValueError(f"Unsupported media type: {pair.media_type}")
        results[pair.media_type].append(
            LabeledScore(
                pair.name,
                pair.expected_match,
                round(similarity, 2),
                note,
                round(raw_similarity, 2),
                eligible,
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or evaluate a labeled media corpus for threshold tuning.")
    parser.add_argument("--manifest", type=Path, help="Optional JSON manifest with labeled image/video/audio pairs.")
    parser.add_argument("--work-dir", type=Path, help="Keep generated synthetic fixtures in this directory.")
    parser.add_argument("--output", type=Path, default=Path("threshold-benchmark.json"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--image-threshold", type=float, default=90.0)
    parser.add_argument("--video-threshold", type=float, default=85.0)
    parser.add_argument("--audio-threshold", type=float, default=94.0)
    args = parser.parse_args()

    ffmpeg, ffprobe = check_video_tools(args.ffmpeg, args.ffprobe)
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg and ffprobe are required for threshold benchmarking.")

    temporary = None
    if args.manifest:
        pairs = _load_manifest(args.manifest)
        corpus_kind = "manifest"
    else:
        if args.work_dir:
            root = args.work_dir.resolve()
            root.mkdir(parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
        pairs = (
            _generate_images(root / "images")
            + _generate_videos(root / "videos", ffmpeg)
            + _generate_audio(root / "audio", ffmpeg)
        )
        corpus_kind = "synthetic"

    scores = _evaluate_pairs(pairs, ffmpeg, ffprobe)
    thresholds = {
        "image": args.image_threshold,
        "video": args.video_threshold,
        "audio": args.audio_threshold,
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": corpus_kind,
        "pair_count": len(pairs),
        "media": {
            media_type: summarize_scores(media_scores, thresholds[media_type])
            for media_type, media_scores in scores.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
