"""GitHub Trending discovery collector."""

from datetime import datetime
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from github_daily_reporter.models import CollectorResult, SourceHealth, SourceObservation
from github_daily_reporter.normalize import extract_repo_ref


TRENDING_URL = "https://github.com/trending"
_COUNT_PATTERN = re.compile(r"\d[\d,]*")
_STARS_TODAY_PATTERN = re.compile(r"stars\s+today", re.IGNORECASE)


class TrendingParseError(ValueError):
    """GitHub Trending HTML did not contain recognizable repository rows."""


def parse_count(text: str) -> int:
    """Extract the first comma-separated integer from GitHub's counter text."""
    match = _COUNT_PATTERN.search(text)
    if match is None:
        raise ValueError("count text did not contain an integer")
    return int(match.group().replace(",", ""))


def _optional_count(node_text: str | None) -> int | None:
    if node_text is None:
        return None
    try:
        return parse_count(node_text)
    except ValueError:
        return None


def parse_trending(html: str, observed_at: datetime) -> list[SourceObservation]:
    """Parse repository observations from a GitHub Trending document."""
    soup = BeautifulSoup(html, "html.parser")
    observations: list[SourceObservation] = []

    for rank, row in enumerate(soup.select("article.Box-row"), start=1):
        link = row.select_one("h2 a")
        if link is None:
            continue
        href = link.get("href")
        if not isinstance(href, str):
            continue
        ref = extract_repo_ref(urljoin(TRENDING_URL, href))
        if ref is None:
            continue

        description = row.select_one("p")
        language = row.find("span", itemprop="programmingLanguage")
        counters = row.select("a.Link--muted")
        daily = row.find(string=_STARS_TODAY_PATTERN)
        observations.append(
            SourceObservation(
                source="trending",
                repository_url=f"https://github.com/{ref.owner}/{ref.name}",
                owner=ref.owner,
                name=ref.name,
                observed_at=observed_at,
                source_rank=rank,
                source_metadata={
                    "description": description.get_text(" ", strip=True) if description else None,
                    "language": language.get_text(" ", strip=True) if language else None,
                    "stars_total": _optional_count(
                        counters[0].get_text(" ", strip=True) if len(counters) > 0 else None
                    ),
                    "forks_total": _optional_count(
                        counters[1].get_text(" ", strip=True) if len(counters) > 1 else None
                    ),
                    "stars_today": _optional_count(daily),
                },
            )
        )

    if not observations:
        raise TrendingParseError("no repository rows recognized in GitHub Trending HTML")
    return observations


async def collect_trending(
    client: httpx.AsyncClient, observed_at: datetime
) -> CollectorResult:
    """Fetch GitHub's daily Trending page and return its repository observations."""
    response = await client.get(TRENDING_URL, params={"since": "daily"})
    response.raise_for_status()
    observations = parse_trending(response.text, observed_at)
    return CollectorResult(
        source="trending",
        observations=observations,
        health=SourceHealth(source="trending", status="success", item_count=len(observations)),
    )
