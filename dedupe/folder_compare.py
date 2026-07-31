from __future__ import annotations

from collections.abc import Callable
from itertools import combinations

from .models import (
    AudioMatch,
    ClusterAssignment,
    ClusterEvidence,
    ContentCluster,
    DedupeReport,
    ExactDuplicateGroup,
    FileRecord,
    FolderPair,
    ImageMatch,
    NormalizedName,
    VideoMatch,
)
from .progress import check_cancelled


def build_content_clusters(
    exact_groups: list[ExactDuplicateGroup],
    video_matches: list[VideoMatch],
    image_matches: list[ImageMatch] | None = None,
    audio_matches: list[AudioMatch] | None = None,
) -> list[ContentCluster]:
    """Union-find over exact groups plus video/image/audio matches.

    Pure function over match lists only (no records/names needed), so callers
    that only have a DedupeReport's raw evidence lists - not the scan's
    FileRecords - can still recompute the same transitive clusters.
    """
    parent: dict[str, str] = {}
    size: dict[str, int] = {}

    def find(path: str) -> str:
        parent.setdefault(path, path)
        size.setdefault(path, 1)
        root = path
        while parent[root] != root:
            root = parent[root]
        while parent[path] != root:
            parent[path], path = root, parent[path]
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if size[left_root] < size[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        size[left_root] += size[right_root]

    for group in exact_groups:
        check_cancelled()
        if not group.paths:
            continue
        first = group.paths[0]
        find(first)
        for path in group.paths[1:]:
            union(first, path)
    for match in video_matches:
        union(match.left, match.right)
    for match in image_matches or []:
        union(match.left, match.right)
    for match in audio_matches or []:
        union(match.left, match.right)

    content_groups: dict[str, list[str]] = {}
    for path in parent:
        content_groups.setdefault(find(path), []).append(path)

    content_clusters: list[ContentCluster] = []
    for paths in content_groups.values():
        check_cancelled()
        if len(paths) < 2:
            continue
        path_set = set(paths)
        component_video_matches = [
            match for match in video_matches if match.left in path_set and match.right in path_set
        ]
        component_image_matches = [
            match for match in image_matches or [] if match.left in path_set and match.right in path_set
        ]
        component_audio_matches = [
            match for match in audio_matches or [] if match.left in path_set and match.right in path_set
        ]
        component_types = sum(
            bool(matches) for matches in (component_video_matches, component_image_matches, component_audio_matches)
        )
        if component_types > 1:
            level = "content"
            confidence = min(
                [match.similarity for match in component_video_matches]
                + [match.similarity for match in component_image_matches]
                + [match.similarity for match in component_audio_matches]
            )
        elif component_video_matches:
            level = "video"
            confidence = min(match.similarity for match in component_video_matches)
        elif component_image_matches:
            level = "image"
            confidence = min(match.similarity for match in component_image_matches)
        elif component_audio_matches:
            level = "audio"
            confidence = min(match.similarity for match in component_audio_matches)
        else:
            level = "exact"
            confidence = 100.0
        cluster_id = f"{level}:{min(paths)}"
        evidence = (
            [
                ClusterEvidence(left=match.left, right=match.right, similarity=match.similarity, level="video")
                for match in component_video_matches
            ]
            + [
                ClusterEvidence(left=match.left, right=match.right, similarity=match.similarity, level="image")
                for match in component_image_matches
            ]
            + [
                ClusterEvidence(left=match.left, right=match.right, similarity=match.similarity, level="audio")
                for match in component_audio_matches
            ]
        )
        content_clusters.append(
            ContentCluster(
                cluster_id=cluster_id,
                level=level,
                confidence=confidence,
                paths=sorted(paths),
                evidence=evidence,
            )
        )

    content_clusters.sort(key=lambda cluster: cluster.cluster_id)
    return content_clusters


def resolve_content_clusters(report: DedupeReport) -> list[ContentCluster]:
    """Return report.content_clusters, recomputing it if a caller built the
    report without going through ScanService (e.g. tests, ad-hoc scripts)."""
    if report.content_clusters:
        return report.content_clusters
    if not (report.exact_duplicates or report.video_matches or report.image_matches or report.audio_matches):
        return []
    return build_content_clusters(
        report.exact_duplicates,
        report.video_matches,
        image_matches=report.image_matches,
        audio_matches=report.audio_matches,
    )


def build_cluster_assignments(
    records: list[FileRecord],
    exact_groups: list[ExactDuplicateGroup],
    video_matches: list[VideoMatch],
    normalized: dict[str, NormalizedName],
    image_matches: list[ImageMatch] | None = None,
    audio_matches: list[AudioMatch] | None = None,
) -> tuple[dict[str, ClusterAssignment], list[ContentCluster]]:
    content_clusters = build_content_clusters(exact_groups, video_matches, image_matches, audio_matches)
    assignments: dict[str, ClusterAssignment] = {}
    for cluster in content_clusters:
        for path in cluster.paths:
            assignments[path] = ClusterAssignment(
                cluster_id=cluster.cluster_id, level=cluster.level, confidence=cluster.confidence
            )

    for record in records:
        check_cancelled()
        if record.path_key in assignments:
            continue
        name = normalized.get(record.path_key) or normalized.get(record.raw_name)
        if name and name.core_title:
            title, year, episode = name.cluster_key
            cluster_id = f"name:{title}|{year}|{episode}"
            assignments[record.path_key] = ClusterAssignment(cluster_id=cluster_id, level="name", confidence=70.0)
        else:
            assignments[record.path_key] = ClusterAssignment(
                cluster_id=f"path:{record.path_key}", level="path", confidence=0.0
            )
    return assignments, content_clusters


def _candidate_folder_pairs(folder_sets: dict[str, set[str]]) -> list[tuple[str, str]]:
    """Folder pairs that share at least one cluster id.

    Comparing every directory pair is quadratic in directory count, and in a
    real library almost all of them share nothing. Build an inverted index
    (cluster id -> folders containing it) instead, so only folders that
    already have something in common become candidates (F-16).
    """
    cluster_to_folders: dict[str, set[str]] = {}
    for folder, cluster_ids in folder_sets.items():
        for cluster_id in cluster_ids:
            cluster_to_folders.setdefault(cluster_id, set()).add(folder)

    candidates: set[tuple[str, str]] = set()
    for folders in cluster_to_folders.values():
        if len(folders) < 2:
            continue
        candidates.update(combinations(sorted(folders), 2))
    return sorted(candidates)


def compare_folders(
    records: list[FileRecord],
    assignments: dict[str, ClusterAssignment],
    threshold: float,
    *,
    key: Callable[[FileRecord], str] | None = None,
    scope: str = "directory",
) -> list[FolderPair]:
    """Compare folders pairwise for shared content/name clusters.

    `key` selects what counts as a "folder" for a given record; it defaults to
    the record's immediate containing directory, so e.g. two differently-named
    copies of the same season folder are compared directly instead of only
    the collection roots they happen to live under (F-16). Pass
    `key=lambda record: record.root_key` for the coarser collection-root
    roll-up the tool used to compute exclusively.
    """
    key_fn = key or (lambda record: str(record.path.parent))
    by_folder: dict[str, list[FileRecord]] = {}
    for record in records:
        by_folder.setdefault(key_fn(record), []).append(record)

    folder_sets: dict[str, set[str]] = {}
    content_folder_sets: dict[str, set[str]] = {}
    cluster_to_paths: dict[tuple[str, str], list[str]] = {}
    for folder, folder_records in by_folder.items():
        check_cancelled()
        ids: set[str] = set()
        content_ids: set[str] = set()
        for record in folder_records:
            assignment = assignments[record.path_key]
            ids.add(assignment.cluster_id)
            if assignment.level in {"exact", "video", "image", "audio", "content"}:
                content_ids.add(assignment.cluster_id)
            else:
                # Unconfirmed files cannot contribute to the content-backed
                # intersection, but they must remain in the union. Otherwise a
                # single confirmed match makes two mostly different folders
                # appear 100% identical.
                content_ids.add(f"unconfirmed:{record.path_key}")
            cluster_to_paths.setdefault((folder, assignment.cluster_id), []).append(record.path_key)
        folder_sets[folder] = ids
        content_folder_sets[folder] = content_ids

    pairs: list[FolderPair] = []
    for left, right in _candidate_folder_pairs(folder_sets):
        check_cancelled()
        left_set = folder_sets[left]
        right_set = folder_sets[right]
        content_left_set = content_folder_sets[left]
        content_right_set = content_folder_sets[right]
        name_union = left_set | right_set
        content_union = content_left_set | content_right_set
        if not name_union:
            continue
        name_intersection = left_set & right_set
        content_intersection = content_left_set & content_right_set
        content_similarity = 100.0 * len(content_intersection) / len(content_union) if content_union else 0.0
        name_assisted_similarity = 100.0 * len(name_intersection) / len(name_union)
        if content_similarity < threshold and name_assisted_similarity < threshold:
            continue
        matched = []
        for cluster_id in sorted(name_intersection):
            sample_path = (
                cluster_to_paths.get((left, cluster_id)) or cluster_to_paths.get((right, cluster_id)) or [""]
            )[0]
            assignment = assignments.get(sample_path, ClusterAssignment(cluster_id, "unknown", 0.0))
            matched.append(
                {
                    "cluster_id": cluster_id,
                    "level": assignment.level,
                    "confidence": assignment.confidence,
                    "content_backed": assignment.level in {"exact", "video", "image", "audio", "content"},
                    "left_paths": sorted(cluster_to_paths.get((left, cluster_id), [])),
                    "right_paths": sorted(cluster_to_paths.get((right, cluster_id), [])),
                }
            )
        left_only = sorted(
            path for cluster_id in left_set - right_set for path in cluster_to_paths.get((left, cluster_id), [])
        )
        right_only = sorted(
            path for cluster_id in right_set - left_set for path in cluster_to_paths.get((right, cluster_id), [])
        )
        pairs.append(
            FolderPair(
                left=left,
                right=right,
                similarity=round(content_similarity, 2),
                content_similarity=round(content_similarity, 2),
                name_assisted_similarity=round(name_assisted_similarity, 2),
                matched=matched,
                left_only=left_only,
                right_only=right_only,
                scope=scope,
            )
        )
    pairs.sort(key=lambda item: (-item.content_similarity, -item.name_assisted_similarity, item.left, item.right))
    return pairs
