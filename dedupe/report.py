from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .folder_compare import resolve_content_clusters
from .models import ContentCluster, DedupeReport, ExactDuplicateGroup


def report_to_dict(report: DedupeReport) -> dict[str, Any]:
    payload = asdict(report)
    if not report.content_clusters:
        payload["content_clusters"] = [asdict(cluster) for cluster in resolve_content_clusters(report)]
    return payload


def write_json_report(report: DedupeReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")


def _path_list(paths: list[str]) -> str:
    if not paths:
        return '<p class="muted">None</p>'
    return "<ul>" + "".join(f"<li><code>{html.escape(path)}</code></li>" for path in paths) + "</ul>"


def _cluster_summary(cluster: ContentCluster, exact_by_paths: dict[frozenset[str], ExactDuplicateGroup]) -> str:
    if cluster.level == "exact":
        group = exact_by_paths.get(frozenset(cluster.paths))
        size_text = f"{group.size:,} bytes" if group is not None else "byte-identical"
        return f"{len(cluster.paths)} files · {size_text}"
    evidence = "; ".join(f"{pair.level} {pair.similarity:.2f}%" for pair in cluster.evidence)
    summary = f"{len(cluster.paths)} files · {cluster.confidence:.2f}% min confidence"
    return f"{summary} · {evidence}" if evidence else summary


def _cluster_items(
    clusters: list[ContentCluster],
    level: str,
    exact_by_paths: dict[frozenset[str], ExactDuplicateGroup],
) -> list[str]:
    items = []
    for cluster in clusters:
        if cluster.level != level:
            continue
        open_attr = " open" if level == "exact" else ""
        items.append(
            f"""
            <details{open_attr}>
              <summary>{html.escape(_cluster_summary(cluster, exact_by_paths))}</summary>
              {_path_list(cluster.paths)}
            </details>
            """
        )
    return items


def write_html_report(report: DedupeReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clusters = resolve_content_clusters(report)
    exact_by_paths = {frozenset(group.paths): group for group in report.exact_duplicates}
    exact_items = _cluster_items(clusters, "exact", exact_by_paths)
    video_items = _cluster_items(clusters, "video", exact_by_paths)
    image_items = _cluster_items(clusters, "image", exact_by_paths)
    audio_items = _cluster_items(clusters, "audio", exact_by_paths)
    content_items = _cluster_items(clusters, "content", exact_by_paths)

    folder_items = []
    for pair in report.folder_pairs:
        matched = "".join(
            f"""
            <details>
              <summary>{html.escape(item["level"])} · {float(item["confidence"]):.2f}% · {html.escape(item["cluster_id"])}</summary>
              <div class="columns">
                <div><h4>Left</h4>{_path_list(item["left_paths"])}</div>
                <div><h4>Right</h4>{_path_list(item["right_paths"])}</div>
              </div>
            </details>
            """
            for item in pair.matched
        )
        folder_items.append(
            f"""
            <details>
              <summary>content {pair.content_similarity:.2f}% · name-assisted {pair.name_assisted_similarity:.2f}% · <code>{html.escape(pair.left)}</code> ↔ <code>{html.escape(pair.right)}</code></summary>
              <h3>Matches</h3>{matched or '<p class="muted">None</p>'}
              <div class="columns">
                <div><h3>Left only</h3>{_path_list(pair.left_only)}</div>
                <div><h3>Right only</h3>{_path_list(pair.right_only)}</div>
              </div>
            </details>
            """
        )

    name_items = []
    for hint in report.name_hints:
        parts = [f"{hint.similarity:.2f}%", html.escape(hint.title)]
        if hint.year:
            parts.append(str(hint.year))
        if hint.episode:
            parts.append(f"episode {hint.episode}")
        name_items.append(
            f"""
            <details>
              <summary>{" · ".join(parts)}</summary>
              {_path_list(hint.paths)}
            </details>
            """
        )

    warnings = (
        _path_list(report.warnings) if report.warnings else '<p class="muted">No warnings captured in report.</p>'
    )
    document = f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Duplicate Media Report</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f7f4;
      --text: #1d2527;
      --muted: #697275;
      --panel: #ffffff;
      --accent: #0f766e;
      --border: #d9dedc;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111618;
        --text: #e9eeee;
        --muted: #a0abad;
        --panel: #192023;
        --accent: #5eead4;
        --border: #334044;
      }}
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    header, main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
    }}
    header {{
      border-bottom: 1px solid var(--border);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 30px;
    }}
    h2 {{
      margin-top: 34px;
      font-size: 22px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin-top: 18px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
    }}
    .stat strong {{
      display: block;
      font-size: 24px;
      color: var(--accent);
    }}
    details {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin: 10px 0;
      padding: 12px 14px;
    }}
    summary {{
      cursor: pointer;
      font-weight: 650;
    }}
    code {{
      overflow-wrap: anywhere;
    }}
    ul {{
      padding-left: 20px;
    }}
    .columns {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }}
    .muted {{
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header>
    <h1>Duplicate Media Report</h1>
    <p class="muted">Scanned {report.scanned_files} files. Exact, video, image, and audio matches are content-backed; name hints and name-assisted folder scores are lower confidence.</p>
    <div class="stats">
      <div class="stat"><strong>{len(exact_items)}</strong>Exact groups</div>
      <div class="stat"><strong>{len(video_items)}</strong>Video clusters</div>
      <div class="stat"><strong>{len(image_items)}</strong>Image clusters</div>
      <div class="stat"><strong>{len(audio_items)}</strong>Audio clusters</div>
      <div class="stat"><strong>{len(content_items)}</strong>Mixed-evidence clusters</div>
      <div class="stat"><strong>{len(report.folder_pairs)}</strong>Folder pairs</div>
      <div class="stat"><strong>{len(report.name_hints)}</strong>Name hints</div>
    </div>
  </header>
  <main>
    <h2>Точні дублікати файлів</h2>
    {"".join(exact_items) or '<p class="muted">No exact duplicates found.</p>'}
    <h2>Схожі відео</h2>
    {"".join(video_items) or '<p class="muted">No similar videos found above threshold.</p>'}
    <h2>Схожі зображення</h2>
    {"".join(image_items) or '<p class="muted">No similar images found above threshold.</p>'}
    <h2>Схожі аудіозаписи</h2>
    {"".join(audio_items) or '<p class="muted">No similar audio found above threshold.</p>'}
    <h2>Змішані докази</h2>
    {"".join(content_items) or '<p class="muted">No mixed-evidence clusters found.</p>'}
    <h2>Пари тек</h2>
    {"".join(folder_items) or '<p class="muted">No folder pairs found above threshold.</p>'}
    <h2>Підказки за назвою</h2>
    {"".join(name_items) or '<p class="muted">No name-only hints found.</p>'}
    <h2>Warnings</h2>
    {warnings}
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
