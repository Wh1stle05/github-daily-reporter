"""Eligibility and deterministic preselection for growth and mature cohorts."""

from collections.abc import Iterable
from datetime import datetime

from github_daily_reporter.models import Cohort, RankedCandidate, RepositoryCandidate
from github_daily_reporter.scoring import (
    cohort_ranking_key,
    score_growth_candidate,
    score_mature_candidate,
)


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


def select_review_candidates(
    candidates: Iterable[RepositoryCandidate],
    now: datetime,
    *,
    growth_cap: int = 20,
    mature_cap: int = 12,
    growth_reserve: int = 4,
) -> dict[Cohort, list[RankedCandidate]]:
    """Score all eligible candidates and return deterministic cohort-local caps.

    Growth reserves are filled from candidates outside the preliminary score cut,
    prioritizing independent evidence and Hacker News engagement.
    """
    grouped: dict[Cohort, list[RankedCandidate]] = {"growth": [], "mature": []}
    seen_names: set[str] = set()
    for candidate in candidates:
        if candidate.canonical_name in seen_names:
            continue
        seen_names.add(candidate.canonical_name)
        cohort = assign_cohort(candidate)
        if cohort is None:
            continue
        score = (
            score_growth_candidate(candidate, now, quality_score=50)
            if cohort == "growth"
            else score_mature_candidate(candidate, now)
        )
        grouped[cohort].append(RankedCandidate(candidate=candidate, score=score))

    growth = sorted(grouped["growth"], key=cohort_ranking_key)
    mature = sorted(grouped["mature"], key=cohort_ranking_key)
    growth_cap = max(growth_cap, 0)
    mature_cap = max(mature_cap, 0)
    reserve = min(max(growth_reserve, 0), growth_cap, len(growth))
    primary = growth[: growth_cap - reserve]
    selected_names = {item.candidate.canonical_name for item in primary}
    reserve_pool = [item for item in growth if item.candidate.canonical_name not in selected_names]
    reserve_pool.sort(
        key=lambda item: (
            -item.score.evidence,
            -item.candidate.hn_points,
            item.candidate.canonical_name,
        )
    )
    selected_growth = primary + reserve_pool[:reserve]
    selected_growth.sort(key=cohort_ranking_key)
    return {"growth": selected_growth[:growth_cap], "mature": mature[:mature_cap]}
