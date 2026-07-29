"""Cohort assignment for repository candidates."""

from datetime import datetime

from github_daily_reporter.models import Cohort, RepositoryCandidate


def assign_cohort(candidate: RepositoryCandidate) -> Cohort | None:
    """Assign each candidate to exactly one cohort, or reject it."""
    if candidate.stars_total >= 10_000:
        return "mature"
    if candidate.stars_total < 1:
        return None
    has_signal = (
        (candidate.stars_24h or 0) > 0
        or candidate.trending_rank is not None
        or candidate.discovery_source_count >= 2
        or (
            "hacker_news" in candidate.discovery_sources
            and (candidate.hn_points > 0 or candidate.hn_comments > 0)
        )
    )
    return "growth" if has_signal else None
