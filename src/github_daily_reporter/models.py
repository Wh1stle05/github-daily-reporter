from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

SourceName = Literal["trending", "github_search", "hacker_news"]
RunStatus = Literal["running", "success", "partial", "failed"]
Cohort = Literal["growth", "mature"]
MomentumSource = Literal["exact", "snapshot_estimate", "trending_proxy", "unknown"]
SCORING_VERSION = "agent-hybrid-v1"


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
    topics: list[str] = Field(default_factory=list)
    stars_total: int = Field(default=0, ge=0)
    forks_total: int = Field(default=0, ge=0)
    open_issues_count: int = Field(default=0, ge=0)
    stars_24h: int | None = Field(default=None, ge=0)
    velocity_rate_24h: float | None = Field(default=None, ge=0)
    stars_24h_estimated: bool = False
    growth_rate_24h: float | None = Field(default=None, ge=0)
    velocity_observed_at: datetime | None = None
    velocity_elapsed_hours: float | None = Field(default=None, ge=0)
    velocity_source: MomentumSource = "unknown"
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


class ScoreBreakdown(BaseModel):
    momentum: float
    evidence: float
    freshness: float
    hacker_news: float
    quality: float
    popularity: float
    final: float


class CohortScoreBreakdown(BaseModel):
    scoring_version: str = SCORING_VERSION
    cohort: Cohort
    momentum_source: MomentumSource
    momentum: float = Field(ge=0)
    relative_growth: float = Field(default=0, ge=0)
    evidence: float = Field(ge=0)
    quality: float = Field(ge=0)
    activity: float = Field(ge=0)
    hacker_news: float = Field(ge=0)
    popularity: float = Field(ge=0)
    final: float = Field(ge=0)


class EditorialCandidate(BaseModel):
    """Compact, immutable Python facts handed to the editorial Agent."""

    canonical_name: str
    full_name: str
    html_url: str
    cohort: Cohort
    python_rank: int = Field(ge=1)
    python_score: float = Field(ge=0, le=100)
    score_breakdown: CohortScoreBreakdown
    stars_total: int = Field(ge=0)
    stars_24h: float | None = Field(default=None, ge=0)
    growth_rate_24h: float | None = Field(default=None, ge=0)
    velocity_source: MomentumSource = "unknown"
    stars_24h_estimated: bool = False
    primary_language: str | None = None
    topics: list[str] = Field(default_factory=list)
    description: str | None = None
    readme_excerpt: str = ""
    created_at: datetime
    pushed_at: datetime | None = None
    discovery_sources: list[SourceName] = Field(default_factory=list)
    source_errors: list[str] = Field(default_factory=list)
    risk_markers: list[str] = Field(default_factory=list)


class EditorialCohort(BaseModel):
    primary: list[EditorialCandidate] = Field(default_factory=list)
    reserve: list[EditorialCandidate] = Field(default_factory=list)


class EditorialInput(BaseModel):
    schema_version: str = SCORING_VERSION
    run_id: str
    generated_at: datetime
    status: Literal["success", "partial"]
    source_health: list[SourceHealth] = Field(default_factory=list)
    available_counts: dict[Cohort, int] = Field(default_factory=dict)
    cohorts: dict[Cohort, EditorialCohort]


class RankedCandidate(BaseModel):
    candidate: RepositoryCandidate
    score: ScoreBreakdown | CohortScoreBreakdown
    quality_degraded: bool = False


class DeliveryPart(BaseModel):
    run_id: str
    part_index: int = Field(ge=0)
    body: str
    digest: str
    attempts: int = Field(default=0, ge=0)
    state: Literal["pending", "in_flight", "delivered"] = "pending"
    claim_token: str | None = None
    claim_deadline: datetime | None = None
    telegram_message_id: str | None = None
    error_category: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CollectionEnvelope(BaseModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    status: RunStatus
    generated_at: datetime
    source_health: list[SourceHealth]
    candidates: list[RepositoryCandidate]
    fatal_error: str | None = None
