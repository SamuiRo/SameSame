from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DedupeReport


def report_to_dict(report: DedupeReport) -> dict[str, Any]:
    return asdict(report)


def write_json_report(report: DedupeReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")


def _path_list(paths: list[str]) -> str:
    if not paths:
        return "<p class=\"muted\">None</p>"
    return "<ul>" + "".join(f"<li><code>{html.escape(path)}</code></li>" for path in paths) + "</ul>"


def write_html_report(report: DedupeReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exact_items = []
    for group in report.exact_duplicates:
        exact_items.append(
            f"""
            <details open>
              <summary>{len(group.paths)} files · {group.similarity:.2f}% · {group.size:,} bytes</summary>
              {_path_list(group.paths)}
            </details>
            """
        )

    video_items = []
    for match in report.video_matches:
        video_items.append(
            f"""
            <details>
              <summary>{match.similarity:.2f}% · Δ {match.duration_delta:.3f}s</summary>
              {_path_list([match.left, match.right])}
            </details>
            """
        )

    folder_items = []
    for pair in report.folder_pairs:
        matched = "".join(
            f"""
            <details>
              <summary>{html.escape(item['level'])} · {float(item['confidence']):.2f}% · {html.escape(item['cluster_id'])}</summary>
              <div class="columns">
                <div><h4>Left</h4>{_path_list(item['left_paths'])}</div>
                <div><h4>Right</h4>{_path_list(item['right_paths'])}</div>
              </div>
            </details>
            """
            for item in pair.matched
        )
        folder_items.append(
            f"""
            <details>
              <summary>{pair.similarity:.2f}% · <code>{html.escape(pair.left)}</code> ↔ <code>{html.escape(pair.right)}</code></summary>
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
              <summary>{' · '.join(parts)}</summary>
              {_path_list(hint.paths)}
            </details>
            """
        )

    warnings = _path_list(report.warnings) if report.warnings else "<p class=\"muted\">No warnings captured in report.</p>"
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
    <p class="muted">Scanned {report.scanned_files} files. Exact and video matches are content-backed; name hints are low confidence.</p>
    <div class="stats">
      <div class="stat"><strong>{len(report.exact_duplicates)}</strong>Exact groups</div>
      <div class="stat"><strong>{len(report.video_matches)}</strong>Video matches</div>
      <div class="stat"><strong>{len(report.folder_pairs)}</strong>Folder pairs</div>
      <div class="stat"><strong>{len(report.name_hints)}</strong>Name hints</div>
    </div>
  </header>
  <main>
    <h2>Точні дублікати файлів</h2>
    {''.join(exact_items) or '<p class="muted">No exact duplicates found.</p>'}
    <h2>Схожі відео</h2>
    {''.join(video_items) or '<p class="muted">No similar videos found above threshold.</p>'}
    <h2>Пари тек</h2>
    {''.join(folder_items) or '<p class="muted">No folder pairs found above threshold.</p>'}
    <h2>Підказки за назвою</h2>
    {''.join(name_items) or '<p class="muted">No name-only hints found.</p>'}
    <h2>Warnings</h2>
    {warnings}
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")

