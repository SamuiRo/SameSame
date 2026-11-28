from __future__ import annotations

import uuid
from pathlib import Path

from ..video_fingerprint import resolve_binary
from .models import MediaInfo, OutputLayout, TranscodePlan
from .presets import TranscodePreset

#: Marks an encode that is staged in the source's own folder while waiting to
#: take the source's place. It never survives a finished REPLACE job.
REPLACE_STAGING_SUFFIX = ".samesame-new"


class PlanError(RuntimeError):
    pass


def _log_path(output_path: Path) -> Path:
    base = output_path.with_suffix(output_path.suffix + ".ffmpeg.log")
    if not base.exists():
        return base
    for index in range(1, 10000):
        candidate = output_path.with_suffix(output_path.suffix + f".ffmpeg.{index}.log")
        if not candidate.exists():
            return candidate
    raise PlanError(f"Cannot allocate a diagnostic log path for {output_path}")


#: Image codecs FFmpeg can write back out as a plain picture file, with the
#: extension and MIME type a Matroska attachment of that picture should carry.
_COVER_FORMATS: dict[str, tuple[str, str]] = {
    "mjpeg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
    "bmp": (".bmp", "image/bmp"),
    "gif": (".gif", "image/gif"),
    "webp": (".webp", "image/webp"),
}


def _cover_art(
    input_info: MediaInfo,
    source: Path,
    output: Path,
    job_id: str,
    executable: str,
) -> tuple[tuple[tuple[str, ...], ...], tuple[Path, ...], tuple[str, ...]]:
    """Carry cover art over as real attachments instead of encoding it.

    Matroska stores cover art (`cover.jpg`) as an attachment, which FFmpeg
    exposes as a one-frame "attached picture" video stream. Mapped with `0:v`,
    that picture would be fed to the preset's video encoder: wasteful at best,
    and a cover with odd dimensions (423x600, say) is rejected outright by 4:2:0
    encoders, failing the whole job with "Invalid argument". Stream-copying it is
    no better, because FFmpeg's Matroska muxer then writes it as a separate
    MJPEG video *track* rather than an attachment.

    So the main command maps only real video (`0:V`), and each picture is first
    extracted byte-for-byte to a scratch file, then re-attached with `-attach`
    under its original name and MIME type. The result has the same layout as
    the source, which is what the validation step's stream count checks.
    """
    attachments_before = input_info.stream_count("attachment")
    preparation: list[tuple[str, ...]] = []
    scratch: list[Path] = []
    attach_args: list[str] = []
    for stream in input_info.streams:
        if stream.kind != "video" or not stream.attached_pic:
            continue
        known = _COVER_FORMATS.get(stream.codec or "")
        if known is None:
            raise PlanError(
                f"Cover art stream #{stream.index} uses {stream.codec or 'an unknown codec'}, "
                "which cannot be carried into the output as an attachment"
            )
        extension, mimetype = known
        number = len(scratch)
        picture = output.with_name(f".{output.stem}.{job_id[:12]}.cover{number}{extension}")
        if picture.exists():
            raise PlanError(f"Temporary cover-art path already exists: {picture}")
        preparation.append(
            (
                executable,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                f"0:{stream.index}",
                "-c",
                "copy",
                "-frames:v",
                "1",
                "-f",
                "image2",
                str(picture),
            )
        )
        scratch.append(picture)
        target = attachments_before + number
        attach_args.extend(
            (
                "-attach",
                str(picture),
                f"-metadata:s:t:{target}",
                f"mimetype={stream.mimetype or mimetype}",
                f"-metadata:s:t:{target}",
                f"filename={stream.filename or f'cover{extension}'}",
            )
        )
    return tuple(preparation), tuple(scratch), tuple(attach_args)


def replacement_target(input_path: Path) -> Path:
    """The path a REPLACE job finally occupies: the source's own, as an MKV."""
    source = input_path.expanduser()
    return source if source.suffix.casefold() == ".mkv" else source.with_suffix(".mkv")


def default_output_path(
    input_path: Path,
    preset_id: str,
    output_dir: Path | None = None,
    *,
    layout: OutputLayout = OutputLayout.BESIDE,
    source_root: Path | None = None,
) -> Path:
    source = input_path.expanduser()
    if layout == OutputLayout.REPLACE:
        # Staged beside the source: the real destination is occupied until the
        # source is cleaned up, and build_plan refuses to aim at an existing file.
        return source.with_name(f"{source.stem}{REPLACE_STAGING_SUFFIX}.mkv")
    if layout == OutputLayout.MIRROR and output_dir is not None:
        relative = Path()
        if source_root is not None:
            try:
                relative = source.resolve().parent.relative_to(source_root.expanduser().resolve())
            except ValueError:
                # Outside the loaded root (added by hand, say): keep it flat
                # rather than inventing a tree the user never asked for.
                relative = Path()
        mirrored = output_dir.expanduser() / relative / f"{source.stem}.mkv"
        # Mirroring can land on the source itself when the output folder is the
        # source folder; fall back to the suffixed name instead of failing.
        if mirrored.expanduser().resolve() != source.resolve():
            return mirrored
    directory = output_dir.expanduser() if output_dir is not None else source.parent
    return directory / f"{source.stem}.{preset_id}.mkv"


def build_plan(
    input_info: MediaInfo,
    output_path: Path,
    preset: TranscodePreset,
    *,
    ffmpeg: str = "ffmpeg",
) -> TranscodePlan:
    executable = resolve_binary(ffmpeg)
    if not executable:
        raise PlanError(f"ffmpeg executable not found: {ffmpeg}")
    source = input_info.path.resolve()
    output = output_path.expanduser().resolve()
    if output.suffix.casefold() != ".mkv":
        raise PlanError("Transcode output must use the .mkv container")
    if source == output:
        raise PlanError("Transcode output cannot overwrite the source file")
    if output.exists():
        raise PlanError(f"Output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    temporary = output.with_name(f".{output.stem}.{job_id[:12]}.part.mkv")
    if temporary.exists():
        raise PlanError(f"Temporary output path already exists: {temporary}")
    preparation, scratch, attach_args = _cover_art(input_info, source, output, job_id, executable)
    command = (
        executable,
        "-hide_banner",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        # Capital V: real video only. Cover art is handled by _cover_art().
        "0:V?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "0:t?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        *preset.video_args,
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-c:t",
        "copy",
        *attach_args,
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary),
    )
    return TranscodePlan(
        job_id=job_id,
        input_path=source,
        output_path=output,
        temporary_path=temporary,
        log_path=_log_path(output),
        preset_id=preset.preset_id,
        input_info=input_info,
        command=command,
        preparation_commands=preparation,
        scratch_paths=scratch,
    )
