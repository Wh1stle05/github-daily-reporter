from collections.abc import Iterable
import re
from urllib.parse import unquote, urlparse

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
    "login",
    "logout",
    "join",
    "new",
    "notifications",
    "dashboard",
    "explore",
    "search",
    "site",
    "organizations",
    "users",
}

OWNER_PATTERN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9._-]+$")


def extract_repo_ref(url: str) -> RepoRef | None:
    """Return the repository named by a GitHub URL, if it names one."""
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname not in {"github.com", "www.github.com"}
            or parsed.port not in {None, 443}
        ):
            return None

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 2:
            return None
        owner, name = (unquote(part) for part in path_parts[:2])
    except ValueError:
        return None

    if owner.lower() in NON_REPOSITORY_PATHS:
        return None
    if name.lower().endswith(".git"):
        name = name[:-4]
    if (
        not OWNER_PATTERN.fullmatch(owner)
        or not REPOSITORY_PATTERN.fullmatch(name)
        or name in {".", ".."}
    ):
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
            _merge_trending_signal(result, observation)
        elif observation.source == "github_search":
            result["search_rank"] = _minimum_rank(
                result.get("search_rank"), observation.source_rank
            )
        elif observation.source == "hacker_news":
            result["hn_points"] = max(
                result["hn_points"], _nonnegative_int(observation.source_metadata.get("points"))
            )
            result["hn_comments"] = max(
                result["hn_comments"], _nonnegative_int(observation.source_metadata.get("comments"))
            )
            item_id = observation.source_metadata.get("item_id")
            if item_id is not None and item_id not in result["hn_item_ids"]:
                result["hn_item_ids"].append(item_id)

    for result in merged.values():
        result["hn_item_ids"].sort()
    return merged


def _minimum_rank(current: int | None, incoming: int | None) -> int | None:
    if current is None:
        return incoming
    if incoming is None:
        return current
    return min(current, incoming)


def _merge_trending_signal(result: dict, observation: SourceObservation) -> None:
    incoming_rank = observation.source_rank
    incoming_stars = observation.source_metadata.get("stars_today")
    current_rank = result.get("trending_rank")

    if "trending_rank" not in result or (
        incoming_rank is not None and (current_rank is None or incoming_rank < current_rank)
    ):
        result["trending_rank"] = incoming_rank
        result["trending_stars_today"] = incoming_stars
    elif incoming_rank == current_rank and _tie_break_value(incoming_stars) > _tie_break_value(
        result.get("trending_stars_today")
    ):
        result["trending_stars_today"] = incoming_stars


def _tie_break_value(value: object) -> tuple[int, int | str]:
    if isinstance(value, int) and not isinstance(value, bool):
        return (1, value)
    return (0, repr(value))


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
