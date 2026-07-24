from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

SourceName = Literal["trending", "github_search", "hacker_news"]
RunStatus = Literal["running", "success", "partial", "failed"]


class RepoRef(BaseModel, frozen=True):
    owner: str
    name: str

    @computed_field
    @property
    def canonical_name(self) -> str:
        return f"{self.owner}/{self.name}".lower()


class SourceObservation(BaseModel):
    source: SourceName
    repository_url: str
    owner: str
    name: str
    observed_at: datetime
    source_rank: int | None = Field(default=None, ge=1)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class SourceHealth(BaseModel):
    source: str
    status: Literal["success", "degraded", "failed"]
    item_count: int = Field(default=0, ge=0)
    error: str | None = None


class CollectorResult(BaseModel):
    source: SourceName
    observations: list[SourceObservation] = Field(default_factory=list)
    health: SourceHealth


class RepositoryCandidate(BaseModel):
    canonical_name: str
    full_name: str
    html_url: str
    description: str | None = None
    created_at: datetime
    pushed_at: datetime | None = None
    archived: bool = False
    disabled: bool = False
    is_fork: bool = False
    is_empty: bool = False
    has_independent_fork_activity: bool = False
    license_spdx: str | None = None
    primary_language: str | None = None
    stars_total: int = Field(default=0, ge=0)
    forks_total: int = Field(default=0, ge=0)
    open_issues_count: int = Field(default=0, ge=0)
    stars_24h: int | None = Field(default=None, ge=0)
    stars_24h_estimated: bool = False
    growth_rate_24h: float | None = Field(default=None, ge=0)
    velocity_hit: bool = False
    trending_rank: int | None = Field(default=None, ge=1)
    trending_stars_today: int | None = Field(default=None, ge=0)
    search_rank: int | None = Field(default=None, ge=1)
    hn_points: int = Field(default=0, ge=0)
    hn_comments: int = Field(default=0, ge=0)
    hn_item_ids: list[int] = Field(default_factory=list)
    discovery_sources: set[SourceName] = Field(default_factory=set)
    quality_evidence: str = ""
    source_errors: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def discovery_source_count(self) -> int:
        return len(self.discovery_sources)


class QualityReview(BaseModel):
    canonical_name: str
    usefulness: int = Field(ge=0, le=5)
    completeness: int = Field(ge=0, le=5)
    novelty: int = Field(ge=0, le=5)
    maintenance: int = Field(ge=0, le=5)
    exclude: bool = False
    exclude_reason: str | None = None
    duplicate_of: str | None = None


class QualityEnvelope(BaseModel):
    run_id: str
    reviews: list[QualityReview]


class ScoreBreakdown(BaseModel):
    momentum: float
    evidence: float
    freshness: float
    hacker_news: float
    quality: float
    popularity: float
    final: float


class RankedCandidate(BaseModel):
    candidate: RepositoryCandidate
    score: ScoreBreakdown
    quality_degraded: bool = False


class CollectionEnvelope(BaseModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    status: RunStatus
    generated_at: datetime
    source_health: list[SourceHealth]
    candidates: list[RepositoryCandidate]
    quality_review_path: str
    fatal_error: str | None = None
