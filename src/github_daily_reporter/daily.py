"""Self-contained daily report orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
import json
from typing import Any

from github_daily_reporter.models import (
    CollectionEnvelope,
    DailyRunResult,
    LlmReview,
    LlmReviewEnvelope,
    RankedCandidate,
    RepositoryCandidate,
    SourceHealth,
)
from github_daily_reporter.quality import deterministic_exclusion
from github_daily_reporter.render import render_failure_alert, render_report
from github_daily_reporter.scoring import cohort_ranking_key, score_growth_candidate, score_mature_candidate
from github_daily_reporter.selection import assign_cohort, select_review_candidates
from github_daily_reporter.state import StateStore
from github_daily_reporter.telegram import split_message


_TELEGRAM_FAILURES = frozenset(
    {
        "timeout",
        "transport",
        "http_status",
        "http_429",
        "http_5xx",
        "invalid_response",
        "message_entry_too_large",
    }
)


class DailyReporter:
    """Run collection, one LLM review, deterministic ranking, and delivery."""

    def __init__(
        self,
        config: Any | None = None,
        collector: Any | None = None,
        llm: Any | None = None,
        telegram: Any | None = None,
        store: StateStore | None = None,
        renderer: Callable[..., str] = render_report,
        *,
        collection: Any | None = None,
        llm_client: Any | None = None,
        telegram_client: Any | None = None,
    ) -> None:
        # The aliases make the boundary explicit while preserving simple test fakes.
        if config is not None and hasattr(config, "collect") and collector is None:
            collector, config = config, None
        self.config = config
        self.collector = collector or collection
        self.llm = llm or llm_client
        self.telegram = telegram or telegram_client
        self.store = store
        self.renderer = renderer
        if self.collector is None or self.llm is None or self.telegram is None or self.store is None:
            raise TypeError("collector, llm, telegram, and store are required")

    async def run(self, now: datetime | None = None) -> DailyRunResult:
        observed_at = _as_utc(now or datetime.now(UTC))
        await self._deliver_pending()
        envelope = await self.collector.collect(observed_at)
        if envelope.status == "failed":
            await self._send_alert(envelope.run_id, "collection", "source_health", envelope.source_health)
            return DailyRunResult(
                run_id=envelope.run_id,
                status="collection_failed",
                source_health=envelope.source_health,
                error_category="source_health",
            )

        candidates = self.store.get_run_candidates(envelope.run_id)
        ranked_at = self.store.get_run_started_at(envelope.run_id)
        reviewable = [candidate for candidate in candidates if deterministic_exclusion(candidate) is None]
        preselected = select_review_candidates(
            reviewable,
            ranked_at,
            growth_cap=_limit(self.config, "growth_review_candidates", 20),
            mature_cap=_limit(self.config, "mature_review_candidates", 12),
            growth_reserve=4,
        )
        review_inputs = [*preselected["growth"], *preselected["mature"]]
        review_input_names = {item.candidate.canonical_name for item in review_inputs}
        try:
            reviewed = await self.llm.review(review_inputs)
            reviews = _reviews(reviewed)
        except Exception as error:
            category = _llm_category(error)
            self.store.save_report_artifacts(
                envelope.run_id,
                _source_json(candidates, envelope.source_health),
                json.dumps({"error_category": category}, separators=(",", ":")),
                "{}",
                "",
                observed_at,
            )
            await self._send_alert(envelope.run_id, "llm", category, envelope.source_health)
            return DailyRunResult(
                run_id=envelope.run_id,
                status="llm_failed",
                source_health=envelope.source_health,
                error_category=category,
            )

        reviews_by_name = {review.canonical_name: review for review in reviews}
        deterministic_exclusions = {
            candidate.canonical_name: reason
            for candidate in candidates
            if (reason := deterministic_exclusion(candidate)) is not None
        }
        excluded_reasons = dict(deterministic_exclusions)
        excluded_reasons.update(
            {
                review.canonical_name: review.exclude_reason or "llm_exclusion"
                for review in reviews
                if review.exclude
            }
        )
        ranked_growth, ranked_mature = _rank_cohorts(
            (
                candidate
                for candidate in candidates
                if candidate.canonical_name in review_input_names
            ),
            ranked_at,
            reviews_by_name,
            excluded_reasons,
        )
        ranked = [*ranked_growth, *ranked_mature]
        self.store.save_ranking(
            envelope.run_id,
            ranked,
            reviews,
            excluded_reasons,
            deterministic_exclusions,
        )
        rendered_growth = [_with_review(item, reviews_by_name.get(item.candidate.canonical_name)) for item in ranked_growth]
        rendered_mature = [_with_review(item, reviews_by_name.get(item.candidate.canonical_name)) for item in ranked_mature]
        markdown = self.renderer(
            observed_at.date(),
            rendered_growth,
            rendered_mature,
            source_health=envelope.source_health,
            growth_limit=_limit(self.config, "growth_report_items", 6),
            mature_limit=_limit(self.config, "mature_report_items", 4),
        )
        self.store.save_report_artifacts(
            envelope.run_id,
            _source_json(candidates, envelope.source_health),
            json.dumps([review.model_dump(mode="json") for review in reviews], ensure_ascii=False, separators=(",", ":")),
            json.dumps(
                {
                    "growth": [item.model_dump(mode="json") for item in ranked_growth],
                    "mature": [item.model_dump(mode="json") for item in ranked_mature],
                    "excluded": excluded_reasons,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            markdown,
            observed_at,
        )
        await self._enqueue_and_deliver(envelope.run_id, markdown)
        pending = bool(self.store.pending_deliveries(envelope.run_id))
        return DailyRunResult(
            run_id=envelope.run_id,
            status="delivery_pending" if pending else "delivered",
            growth=ranked_growth[: _limit(self.config, "growth_report_items", 6)],
            mature=ranked_mature[: _limit(self.config, "mature_report_items", 4)],
            markdown=markdown,
            source_health=envelope.source_health,
            error_category="delivery" if pending else None,
        )

    async def _deliver_pending(self) -> None:
        blocked_runs: set[str] = set()
        for part in self.store.pending_deliveries():
            if part.run_id in blocked_runs:
                continue
            claimed = self.store.claim_delivery(part.run_id, part.part_index)
            if claimed is None or claimed.claim_token is None:
                blocked_runs.add(part.run_id)
                continue
            result = await _telegram_send(self.telegram, claimed.body)
            if _telegram_ok(result):
                self.store.mark_delivery_delivered(
                    claimed.run_id, claimed.part_index, str(result), claimed.claim_token
                )
            else:
                self.store.mark_delivery_pending(
                    claimed.run_id, claimed.part_index, str(result), claimed.claim_token
                )
                blocked_runs.add(part.run_id)

    async def _enqueue_and_deliver(self, run_id: str, markdown: str) -> None:
        try:
            parts = split_message(markdown)
        except ValueError as error:
            self.store.enqueue_delivery_batch(run_id, [(0, markdown)])
            self.store.mark_delivery_pending(run_id, 0, str(error))
            return
        self.store.enqueue_delivery_batch(run_id, list(enumerate(parts)))
        await self._deliver_pending_for_run(run_id)

    async def _deliver_pending_for_run(self, run_id: str) -> None:
        for part in self.store.pending_deliveries(run_id):
            claimed = self.store.claim_delivery(part.run_id, part.part_index)
            if claimed is None or claimed.claim_token is None:
                break
            result = await _telegram_send(self.telegram, claimed.body)
            if _telegram_ok(result):
                self.store.mark_delivery_delivered(run_id, part.part_index, str(result), claimed.claim_token)
            else:
                self.store.mark_delivery_pending(run_id, part.part_index, str(result), claimed.claim_token)
                break

    async def _send_alert(
        self, run_id: str, phase: str, category: str, source_health: Iterable[SourceHealth]
    ) -> None:
        try:
            await self.telegram.send(render_failure_alert(run_id, phase, category, source_health))
        except Exception:
            return


def _rank_cohorts(
    candidates: Iterable[RepositoryCandidate],
    now: datetime,
    reviews: dict[str, LlmReview],
    exclusions: dict[str, str],
) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
    growth: list[RankedCandidate] = []
    mature: list[RankedCandidate] = []
    for candidate in candidates:
        if candidate.canonical_name in exclusions:
            continue
        cohort = assign_cohort(candidate)
        if cohort is None:
            continue
        review = reviews.get(candidate.canonical_name)
        if cohort == "growth":
            quality = review.quality_score if review is not None else 50
            item = RankedCandidate(
                candidate=candidate,
                score=score_growth_candidate(candidate, now, quality),
                quality_degraded=review is None,
            )
            growth.append(item)
        else:
            mature.append(
                RankedCandidate(candidate=candidate, score=score_mature_candidate(candidate, now))
            )
    growth.sort(key=cohort_ranking_key)
    mature.sort(key=cohort_ranking_key)
    return growth, mature


def _with_review(item: RankedCandidate, review: LlmReview | None) -> dict[str, Any]:
    return {"candidate": item.candidate, "score": item.score, "review": review}


def _reviews(value: LlmReviewEnvelope | Iterable[LlmReview]) -> list[LlmReview]:
    if isinstance(value, LlmReviewEnvelope):
        return value.reviews
    return list(value)


def _llm_category(error: Exception) -> str:
    category = getattr(error, "category", None)
    if isinstance(category, str) and category in {
        "timeout",
        "transport",
        "http_status",
        "invalid_json",
        "invalid_schema",
        "identity_mismatch",
    }:
        return category
    if isinstance(error, (TimeoutError,)):
        return "timeout"
    return "transport"


def _telegram_ok(value: Any) -> bool:
    return value is not None and str(value) not in _TELEGRAM_FAILURES


async def _telegram_send(telegram: Any, body: str) -> Any:
    try:
        return await telegram.send(body)
    except Exception as error:
        category = getattr(error, "category", None)
        if category in _TELEGRAM_FAILURES:
            return category
        if isinstance(error, TimeoutError):
            return "timeout"
        return "transport"


def _limit(config: Any | None, name: str, default: int) -> int:
    value = getattr(config, name, default) if config is not None else default
    return int(value)


def _source_json(candidates: Iterable[RepositoryCandidate], health: Iterable[SourceHealth]) -> str:
    return json.dumps(
        {
            "source_health": [item.model_dump(mode="json") for item in health],
            "candidates": [item.model_dump(mode="json") for item in candidates],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
