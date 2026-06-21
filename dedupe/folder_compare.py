from __future__ import annotations

from itertools import combinations

from .models import ClusterAssignment, ExactDuplicateGroup, FileRecord, FolderPair, NormalizedName, VideoMatch


def build_cluster_assignments(
    records: list[FileRecord],
    exact_groups: list[ExactDuplicateGroup],
    video_matches: list[VideoMatch],
    normalized: dict[str, NormalizedName],
) -> dict[str, ClusterAssignment]:
    assignments: dict[str, ClusterAssignment] = {}

    for index, group in enumerate(exact_groups, start=1):
        cluster_id = f"exact:{group.hash_value}"
        for path in group.paths:
            assignments[path] = ClusterAssignment(cluster_id=cluster_id, level="exact", confidence=100.0)

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

    for match in video_matches:
        union(match.left, match.right)

    video_groups: dict[str, list[str]] = {}
    for path in parent:
        video_groups.setdefault(find(path), []).append(path)
    for root, paths in video_groups.items():
        cluster_id = "video:" + root
        confidence = min(
            [match.similarity for match in video_matches if match.left in paths or match.right in paths],
            default=90.0,
        )
        for path in paths:
            assignments.setdefault(path, ClusterAssignment(cluster_id=cluster_id, level="video", confidence=confidence))

    for record in records:
        if record.path_key in assignments:
            continue
        name = normalized.get(record.raw_name)
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
    cluster_to_paths: dict[tuple[str, str], list[str]] = {}
    for folder, folder_records in by_folder.items():
        ids: set[str] = set()
        for record in folder_records:
            assignment = assignments[record.path_key]
            ids.add(assignment.cluster_id)
            cluster_to_paths.setdefault((folder, assignment.cluster_id), []).append(record.path_key)
        folder_sets[folder] = ids

    pairs: list[FolderPair] = []
    for left, right in combinations(sorted(folder_sets), 2):
        left_set = folder_sets[left]
        right_set = folder_sets[right]
        union = left_set | right_set
        if not union:
            continue
        intersection = left_set & right_set
        similarity = 100.0 * len(intersection) / len(union)
        if similarity < threshold:
            continue
        matched = []
        for cluster_id in sorted(intersection):
            sample_path = (cluster_to_paths.get((left, cluster_id)) or cluster_to_paths.get((right, cluster_id)) or [""])[0]
            assignment = assignments.get(sample_path, ClusterAssignment(cluster_id, "unknown", 0.0))
            matched.append(
                {
                    "cluster_id": cluster_id,
                    "level": assignment.level,
                    "confidence": assignment.confidence,
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
                similarity=round(similarity, 2),
                matched=matched,
                left_only=left_only,
                right_only=right_only,
            )
        )
    pairs.sort(key=lambda item: (-item.similarity, item.left, item.right))
    return pairs

