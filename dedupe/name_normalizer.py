from __future__ import annotations

import logging
import os
import re
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from .cache import Cache
from .models import FileRecord, NameHint, NormalizedName
from .progress import tqdm

try:
    from rapidfuzz import fuzz
except ImportError:
    import difflib

    class _FuzzFallback:
        @staticmethod
        def token_set_ratio(left: str, right: str) -> float:
            left_tokens = set(left.casefold().split())
            right_tokens = set(right.casefold().split())
            common = " ".join(sorted(left_tokens & right_tokens))
            left_remainder = " ".join(sorted(left_tokens - right_tokens))
            right_remainder = " ".join(sorted(right_tokens - left_tokens))
            left_cmp = " ".join(part for part in (common, left_remainder) if part)
            right_cmp = " ".join(part for part in (common, right_remainder) if part)
            return 100.0 * difflib.SequenceMatcher(None, left_cmp, right_cmp).ratio()

    fuzz = _FuzzFallback()

LOGGER = logging.getLogger(__name__)
MODEL_NAME = "claude-haiku-4-5"
LMSTUDIO_URL = "http://localhost:1234/v1"
LMSTUDIO_MODEL = "local-model"
BATCH_SIZE = 150

SYSTEM_PROMPT = """Ти отримуєш список назв медіафайлів (аніме, серіали, фільми) у вигляді
масиву {id, raw}. У назвах може бути:
- підкреслення замість пробілів, крапки замість пробілів
- тег фансаб/реліз-групи у [квадратних] чи (круглих) дужках —
  назва команди може бути будь-якою, не намагайся її впізнати
- мітки SUB/DUB/якості/кодеку/роздільної здатності (1080p, x264, repack...)
- переклад назви іншою мовою поруч з оригінальною/романізованою назвою

Для кожного елемента визнач:
- core_title: основна ідентифікуюча назва твору. Якщо в рядку є і переклад,
  і оригінальна/романізована назва — залиш романізовану (вона стабільніша
  для зіставлення між різними джерелами), переклад відкинь. Прибери все
  стороннє: групу релізу, мітки якості/субтитрів/кодеку.
- year: рік випуску, якщо явно вказаний, інакше null.
- episode: номер серії, якщо явно вказаний, інакше null.
- flags: короткі мітки кшталту "sub", "dub", "ova", якщо присутні.

Не вигадуй те, чого немає в рядку. Якщо назва вже чиста — просто прибери
підкреслення/крапки і зайві пробіли.
"""

TOOL_SCHEMA = {
    "name": "normalize_titles",
    "description": "Нормалізовані канонічні назви для списку файлів",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "core_title": {"type": "string"},
                        "year": {"type": ["integer", "null"]},
                        "episode": {"type": ["integer", "null"]},
                        "flags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "core_title"],
                },
            }
        },
        "required": ["results"],
    },
}

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "core_title": {"type": "string"},
                    "year": {"type": ["integer", "null"]},
                    "episode": {"type": ["integer", "null"]},
                    "flags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "core_title"],
            },
        }
    },
    "required": ["results"],
}

QUALITY_RE = re.compile(
    r"\b(480p|720p|1080p|2160p|4k|8k|x264|x265|h264|h265|hevc|aac|flac|opus|web-dl|webrip|bdrip|bluray|hdrip|dvdrip|repack|proper|sub|subs|dub|dual audio)\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")
EPISODE_RE = re.compile(r"(?:^|[\s._-])(?:e|ep|episode)?\s*0*(\d{1,4})(?:v\d+)?(?:[\s._-]|$)", re.IGNORECASE)


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def fallback_normalize(raw_name: str) -> NormalizedName:
    working = raw_name.replace("_", " ").replace(".", " ")
    flags = [flag for flag in ("sub", "dub", "ova") if re.search(rf"\b{flag}\b", working, re.IGNORECASE)]
    year_match = YEAR_RE.search(working)
    year = int(year_match.group(1)) if year_match else None
    episode_match = EPISODE_RE.search(working)
    episode = int(episode_match.group(1)) if episode_match else None
    working = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", working)
    working = QUALITY_RE.sub(" ", working)
    working = YEAR_RE.sub(" ", working)
    if episode_match:
        working = EPISODE_RE.sub(" ", working, count=1)
    working = re.sub(r"[-–—|]+", " ", working)
    working = re.sub(r"\s+", " ", working).strip()
    return NormalizedName(
        raw_name=raw_name,
        core_title=working or raw_name,
        year=year,
        episode=episode,
        flags=sorted(set(flags)),
        source="fallback",
    )


def _normalize_batch_with_claude(batch: list[str]) -> list[NormalizedName]:
    from anthropic import Anthropic  # type: ignore

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    payload = [{"id": idx, "raw": raw} for idx, raw in enumerate(batch)]
    response = client.messages.create(
        model=MODEL_NAME,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "normalize_titles"},
        messages=[{"role": "user", "content": f"Нормалізуй ці назви:\n{payload!r}"}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "normalize_titles":
            results = block.input.get("results", [])
            output: list[NormalizedName] = []
            by_id = {idx: raw for idx, raw in enumerate(batch)}
            for item in results:
                raw = by_id.get(int(item["id"]))
                if raw is None:
                    continue
                output.append(
                    NormalizedName(
                        raw_name=raw,
                        core_title=str(item.get("core_title") or raw).strip(),
                        year=item.get("year"),
                        episode=item.get("episode"),
                        flags=[str(flag).casefold() for flag in item.get("flags", [])],
                        source="claude",
                    )
                )
            return output
    raise RuntimeError("Claude did not return normalize_titles tool output")


def _coerce_normalized_results(batch: list[str], results: object, source: str) -> list[NormalizedName]:
    if not isinstance(results, list):
        raise ValueError("AI response field 'results' must be a list")
    output: list[NormalizedName] = []
    by_id = {idx: raw for idx, raw in enumerate(batch)}
    for item in results:
        if not isinstance(item, dict):
            continue
        raw = by_id.get(int(item.get("id", -1)))
        if raw is None:
            continue
        output.append(
            NormalizedName(
                raw_name=raw,
                core_title=str(item.get("core_title") or raw).strip(),
                year=item.get("year") if isinstance(item.get("year"), int) else None,
                episode=item.get("episode") if isinstance(item.get("episode"), int) else None,
                flags=[str(flag).casefold() for flag in item.get("flags", []) if isinstance(flag, str)],
                source=source,
            )
        )
    missing = set(batch) - {item.raw_name for item in output}
    output.extend(fallback_normalize(raw) for raw in missing)
    return output


def _extract_json_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("AI response must be a JSON object")
    return value


def _normalize_batch_with_lmstudio(
    batch: list[str],
    base_url: str = LMSTUDIO_URL,
    model: str = LMSTUDIO_MODEL,
    timeout: float = 120.0,
) -> list[NormalizedName]:
    payload = [{"id": idx, "raw": raw} for idx, raw in enumerate(batch)]
    base_url = base_url.rstrip("/")
    request_payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Нормалізуй ці назви. Поверни лише валідний JSON-об'єкт без Markdown. "
                    "Формат: {\"results\":[{\"id\":0,\"core_title\":\"...\",\"year\":null,"
                    "\"episode\":null,\"flags\":[]}]}\n"
                    f"{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "normalize_titles",
                "schema": JSON_SCHEMA,
            },
        },
    }
    data = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Older OpenAI-compatible local servers may reject json_schema. Retry with json_object.
        if exc.code >= 400:
            request_payload["response_format"] = {"type": "json_object"}
            data = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        else:
            raise
    content = response_data["choices"][0]["message"]["content"]
    parsed = _extract_json_object(str(content))
    return _coerce_normalized_results(batch, parsed.get("results"), source="lmstudio")


def normalize_names(
    records: list[FileRecord],
    cache: Cache,
    name_provider: str = "auto",
    lmstudio_url: str = LMSTUDIO_URL,
    lmstudio_model: str = LMSTUDIO_MODEL,
    workers: int = 3,
) -> dict[str, NormalizedName]:
    raw_names = sorted({record.raw_name for record in records})
    normalized: dict[str, NormalizedName] = {}
    missing: list[str] = []
    for raw_name in raw_names:
        cached = cache.get_name(raw_name)
        if cached:
            normalized[raw_name] = cached
        else:
            missing.append(raw_name)

    provider = name_provider.casefold()
    if provider == "none":
        selected_provider = "fallback"
    elif provider == "anthropic":
        selected_provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "fallback"
        if selected_provider == "fallback":
            LOGGER.warning("ANTHROPIC_API_KEY is not set; using local heuristic name normalization.")
    elif provider == "lmstudio":
        selected_provider = "lmstudio"
    elif provider == "auto":
        selected_provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "fallback"
    else:
        LOGGER.warning("Unknown name provider '%s'; using local heuristic name normalization.", name_provider)
        selected_provider = "fallback"

    if missing and selected_provider in {"anthropic", "lmstudio"}:
        batches = list(_chunks(missing, BATCH_SIZE))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            if selected_provider == "anthropic":
                futures = {executor.submit(_normalize_batch_with_claude, batch): batch for batch in batches}
            else:
                futures = {
                    executor.submit(_normalize_batch_with_lmstudio, batch, lmstudio_url, lmstudio_model): batch
                    for batch in batches
                }
            for future in tqdm(as_completed(futures), total=len(futures), desc=f"{selected_provider} name normalization", unit="batch"):
                batch = futures[future]
                try:
                    batch_results = future.result()
                except Exception as exc:  # noqa: BLE001 - keep the scan usable if an API batch fails.
                    LOGGER.warning("%s normalization failed for one batch: %s", selected_provider, exc)
                    batch_results = [fallback_normalize(raw) for raw in batch]
                cache.upsert_names(batch_results)
                normalized.update({item.raw_name: item for item in batch_results})
    elif missing:
        fallback_results = [fallback_normalize(raw) for raw in tqdm(missing, desc="Name normalization", unit="name")]
        cache.upsert_names(fallback_results)
        normalized.update({item.raw_name: item for item in fallback_results})

    return normalized


def find_name_hints(
    records: list[FileRecord],
    normalized: dict[str, NormalizedName],
    exact_cluster_paths: set[str],
    video_cluster_paths: set[str],
    fuzzy_threshold: float = 92.0,
) -> list[NameHint]:
    confirmed_paths = exact_cluster_paths | video_cluster_paths
    by_key: dict[tuple[str, int | None, int | None], list[FileRecord]] = {}
    for record in records:
        name = normalized.get(record.raw_name)
        if name is None or not name.core_title.strip():
            continue
        by_key.setdefault(name.cluster_key, []).append(record)

    hints: list[NameHint] = []
    for key, group in by_key.items():
        unconfirmed = [record for record in group if record.path_key not in confirmed_paths]
        if len(unconfirmed) > 1:
            name = normalized[unconfirmed[0].raw_name]
            hints.append(
                NameHint(
                    key="name:" + "|".join(str(part) for part in key),
                    similarity=100.0,
                    paths=sorted(record.path_key for record in unconfirmed),
                    title=name.core_title,
                    year=name.year,
                    episode=name.episode,
                )
            )

    names = list(by_key.keys())
    for left_index, left_key in enumerate(names):
        for right_key in names[left_index + 1 :]:
            left_title, left_year, left_episode = left_key
            right_title, right_year, right_episode = right_key
            if left_year != right_year or left_episode != right_episode:
                continue
            similarity = float(fuzz.token_set_ratio(left_title, right_title))
            if fuzzy_threshold <= similarity < 100.0:
                paths = [
                    record.path_key
                    for record in by_key[left_key] + by_key[right_key]
                    if record.path_key not in confirmed_paths
                ]
                if len(paths) > 1:
                    hints.append(
                        NameHint(
                            key=f"fuzzy:{left_title}|{right_title}|{left_year}|{left_episode}",
                            similarity=round(similarity, 2),
                            paths=sorted(paths),
                            title=f"{left_title} / {right_title}",
                            year=left_year,
                            episode=left_episode,
                        )
                    )
    hints.sort(key=lambda item: (-item.similarity, item.title, item.paths))
    return hints
