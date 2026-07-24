from collections.abc import Iterable
from urllib.parse import urlparse

from github_daily_reporter.models import RepoRef, SourceObservation


NON_REPOSITORY_PATHS = {
    "about",
    "apps",
    "collections",
    "enterprise",
    "events",
    "features",
    "marketplace",
    "orgs",
    "settings",
    "sponsors",
    "topics",
}


def extract_repo_ref(url: str) -> RepoRef | None:
    """Return the repository named by a GitHub URL, if it names one."""
    parsed = urlparse(url)
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[0].lower() in NON_REPOSITORY_PATHS:
        return None

    owner, name = path_parts[:2]
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not owner or not name:
        return None
    return RepoRef(owner=owner, name=name)


def merge_observations(observations: Iterable[SourceObservation]) -> dict[str, dict]:
    """Merge discovery observations for each case-insensitive repository identity."""
    merged: dict[str, dict] = {}
    for observation in observations:
        repo = RepoRef(owner=observation.owner, name=observation.name)
        canonical_name = repo.canonical_name
        result = merged.setdefault(
            canonical_name,
            {
                "discovery_sources": set(),
                "hn_item_ids": [],
                "hn_points": 0,
                "hn_comments": 0,
            },
        )
        result["discovery_sources"].add(observation.source)

        if observation.source == "trending":
            result["trending_rank"] = observation.source_rank
            result["trending_stars_today"] = observation.source_metadata.get("stars_today")
        elif observation.source == "github_search":
            result["search_rank"] = observation.source_rank
        elif observation.source == "hacker_news":
            result["hn_points"] = max(result["hn_points"], observation.source_metadata.get("points", 0))
            result["hn_comments"] = max(
                result["hn_comments"], observation.source_metadata.get("comments", 0)
            )
            item_id = observation.source_metadata.get("item_id")
            if item_id is not None and item_id not in result["hn_item_ids"]:
                result["hn_item_ids"].append(item_id)

    return merged
