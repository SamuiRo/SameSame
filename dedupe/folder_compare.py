from __future__ import annotations

from itertools import combinations

from .models import (
    AudioMatch,
    ClusterAssignment,
    ExactDuplicateGroup,
    FileRecord,
    FolderPair,
    ImageMatch,
    NormalizedName,
    VideoMatch,
)
from .progress import check_cancelled


def build_cluster_assignments(
    records: list[FileRecord],
    exact_groups: list[ExactDuplicateGroup],
    video_matches: list[VideoMatch],
    normalized: dict[str, NormalizedName],
    image_matches: list[ImageMatch] | None = None,
    audio_matches: list[AudioMatch] | None = None,
) -> dict[str, ClusterAssignment]:
    assignments: dict[str, ClusterAssignment] = {}
    parent: dict[str, str] = {}

    def find(path: str) -> str:
        parent.setdefault(path, path)
        if parent[path] != path:
            parent[path] = find(parent[path])
        return parent[path]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

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
    for paths in content_groups.values():
        check_cancelled()
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
            bool(matches)
            for matches in (component_video_matches, component_image_matches, component_audio_matches)
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
        for path in paths:
            assignments[path] = ClusterAssignment(cluster_id=cluster_id, level=level, confidence=confidence)

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
            assignments[record.path_key] = ClusterAssignment(cluster_id=f"path:{record.path_key}", level="path", confidence=0.0)
    return assignments


def compare_folders(
    records: list[FileRecord],
    assignments: dict[str, ClusterAssignment],
    threshold: float,
) -> list[FolderPair]:
    by_folder: dict[str, list[FileRecord]] = {}
    for record in records:
        by_folder.setdefault(record.root_key, []).append(record)

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
    for left, right in combinations(sorted(folder_sets), 2):
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
            sample_path = (cluster_to_paths.get((left, cluster_id)) or cluster_to_paths.get((right, cluster_id)) or [""])[0]
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
        left_only = sorted(path for cluster_id in left_set - right_set for path in cluster_to_paths.get((left, cluster_id), []))
        right_only = sorted(path for cluster_id in right_set - left_set for path in cluster_to_paths.get((right, cluster_id), []))
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
            )
        )
    pairs.sort(key=lambda item: (-item.content_similarity, -item.name_assisted_similarity, item.left, item.right))
    return pairs
