from datetime import UTC, datetime
import json
import sqlite3

import pytest

from github_daily_reporter.daily import DailyReporter
from github_daily_reporter.models import (
    CollectionEnvelope,
    LlmReview,
    LlmReviewEnvelope,
    SourceHealth,
    RepositoryCandidate,
)
from github_daily_reporter.state import StateStore


NOW = datetime(2026, 7, 27, 9, tzinfo=UTC)


def _candidate(name: str, stars: int, **extra) -> RepositoryCandidate:
    return RepositoryCandidate(
        canonical_name=name,
        full_name=name,
        html_url=f"https://github.com/{name}",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        stars_total=stars,
        stars_24h=5,
        discovery_sources={"trending"},
        **extra,
    )


class FakeCollector:
    def __init__(self, store: StateStore, candidates, status="success"):
        self.store = store
        self.candidates = candidates
        self.status = status

    async def collect(self, now=None):
        run_id = self.store.start_run(now or NOW)
        if self.status == "failed":
            health = [SourceHealth(source="trending", status="failed", error="hidden")]
            self.store.finish_run(run_id, "failed", health, "collection failed")
            return CollectionEnvelope(
                run_id=run_id,
                status="failed",
                generated_at=now or NOW,
                source_health=health,
                candidates=[],
                quality_review_path=f"data/runs/{run_id}/quality-review.json",
                fatal_error="collection failed",
            )
        self.store.save_collection(run_id, self.candidates, [])
        health = [SourceHealth(source="trending", status="success", item_count=len(self.candidates))]
        self.store.finish_run(run_id, self.status, health)
        return CollectionEnvelope(
            run_id=run_id,
            status=self.status,
            generated_at=now or NOW,
            source_health=health,
            candidates=[],
            quality_review_path=f"data/runs/{run_id}/quality-review.json",
        )


class FakeLlm:
    def __init__(self, fail=None, reviews=None, quality_score=80):
        self.calls = []
        self.fail = fail
        self.reviews = reviews
        self.quality_score = quality_score

    async def review(self, candidates):
        candidates = list(candidates)
        self.calls.append(candidates)
        if self.fail:
            from github_daily_reporter.llm import LlmReviewError

            raise LlmReviewError(self.fail)
        if self.reviews is not None:
            return LlmReviewEnvelope(reviews=self.reviews)
        return LlmReviewEnvelope(
            reviews=[
                LlmReview(
                    canonical_name=item.candidate.canonical_name if hasattr(item, "candidate") else item.canonical_name,
                    quality_score=self.quality_score,
                    summary_zh="summary",
                    highlight_zh="highlight",
                )
                for item in candidates
            ]
        )


class FakeTelegram:
    def __init__(self, failures=0):
        self.sent = []
        self.failures = failures

    async def send(self, text):
        self.sent.append(text)
        if self.failures:
            self.failures -= 1
            return "timeout"
        return str(len(self.sent))


@pytest.mark.asyncio
async def test_daily_reviews_once_ranks_and_delivers(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    candidates = [_candidate("growth/repo", 100), _candidate("mature/repo", 10_000)]
    llm = FakeLlm()
    telegram = FakeTelegram()
    reporter = DailyReporter(
        collector=FakeCollector(store, candidates),
        llm=llm,
        telegram=telegram,
        store=store,
    )

    result = await reporter.run(now=NOW)

    assert result.status == "delivered"
    assert len(llm.calls) == 1
    assert len(result.growth) <= 6
    assert len(result.mature) <= 4
    assert telegram.sent


@pytest.mark.asyncio
async def test_llm_timeout_sends_sanitized_alert_without_report(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    llm = FakeLlm("timeout")
    telegram = FakeTelegram()
    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=llm,
        telegram=telegram,
        store=store,
    )

    result = await reporter.run(now=NOW)

    assert result.status == "llm_failed"
    assert len(llm.calls) == 1
    assert "timeout" in telegram.sent[-1]
    assert "sk-" not in telegram.sent[-1]
    assert result.markdown is None


@pytest.mark.asyncio
async def test_pending_delivery_is_recovered_before_new_run(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.enqueue_delivery("old-run", 0, "old report")
    telegram = FakeTelegram()
    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=FakeLlm(),
        telegram=telegram,
        store=store,
    )

    await reporter.run(now=NOW)

    assert telegram.sent[0] == "old report"
    assert store.pending_deliveries("old-run") == []


@pytest.mark.asyncio
async def test_collection_failure_excludes_llm(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    llm = FakeLlm(quality_score=0)
    telegram = FakeTelegram()
    reporter = DailyReporter(
        collector=FakeCollector(store, [], status="failed"),
        llm=llm,
        telegram=telegram,
        store=store,
    )

    result = await reporter.run(now=NOW)

    assert result.status == "collection_failed"
    assert llm.calls == []
    assert "trending" in telegram.sent[-1]


@pytest.mark.asyncio
async def test_daily_uses_all_persisted_candidates_not_bounded_envelope(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    candidates = [
        _candidate("growth/from-state", 100),
        _candidate("mature/from-state", 10_000),
    ]
    llm = FakeLlm()
    reporter = DailyReporter(
        collector=FakeCollector(store, candidates),
        llm=llm,
        telegram=FakeTelegram(),
        store=store,
    )

    result = await reporter.run(now=NOW)

    assert result.status == "delivered"
    assert {item.candidate.canonical_name for item in llm.calls[0]} == {
        "growth/from-state",
        "mature/from-state",
    }


@pytest.mark.asyncio
async def test_mature_llm_quality_is_exclusion_only(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    included = _candidate("mature/included", 10_000)
    excluded = _candidate("mature/excluded", 20_000)
    llm = FakeLlm(
        reviews=[
            LlmReview(
                canonical_name=included.canonical_name,
                quality_score=100,
                summary_zh="summary",
                highlight_zh="highlight",
            ),
            LlmReview(
                canonical_name=excluded.canonical_name,
                quality_score=0,
                exclude=True,
                exclude_reason="not useful",
                summary_zh="summary",
                highlight_zh="highlight",
            ),
        ]
    )
    reporter = DailyReporter(
        collector=FakeCollector(store, [included, excluded]),
        llm=llm,
        telegram=FakeTelegram(),
        store=store,
    )

    result = await reporter.run(now=NOW)

    assert [item.candidate.canonical_name for item in result.mature] == [included.canonical_name]
    assert result.mature[0].score.quality == 0


@pytest.mark.asyncio
async def test_llm_failure_persists_only_sanitized_error_artifact(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")

    class UnsafeLlm:
        async def review(self, candidates):
            raise RuntimeError("Authorization: Bearer sk-secret")

    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=UnsafeLlm(),
        telegram=FakeTelegram(),
        store=store,
    )

    result = await reporter.run(now=NOW)

    with sqlite3.connect(store.path) as connection:
        review_json, markdown = connection.execute(
            "SELECT review_json, markdown FROM report_artifacts WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    assert json.loads(review_json) == {"error_category": "transport"}
    assert markdown == ""


@pytest.mark.asyncio
async def test_final_telegram_failure_keeps_report_queued(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    telegram = FakeTelegram(failures=1)
    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=FakeLlm(),
        telegram=telegram,
        store=store,
    )

    result = await reporter.run(now=NOW)

    pending = store.pending_deliveries(result.run_id)
    assert result.status == "delivery_pending"
    assert result.error_category == "delivery"
    assert len(pending) == 1
    assert pending[0].attempts == 1


@pytest.mark.asyncio
async def test_daily_renders_only_candidates_reviewed_by_the_single_llm_call(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    candidates = [_candidate(f"growth/{index:02d}", 100) for index in range(21)]
    llm = FakeLlm(quality_score=0)
    reporter = DailyReporter(
        collector=FakeCollector(store, candidates),
        llm=llm,
        telegram=FakeTelegram(),
        store=store,
    )

    result = await reporter.run(now=NOW)

    reviewed_names = {item.candidate.canonical_name for item in llm.calls[0]}
    rendered_names = {item.candidate.canonical_name for item in result.growth}
    assert len(reviewed_names) == 20
    assert rendered_names <= reviewed_names


@pytest.mark.asyncio
async def test_multi_part_delivery_stops_after_the_first_failed_part(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    first_part = "a" * 3_000
    second_part = "b" * 3_000
    telegram = FakeTelegram(failures=1)
    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=FakeLlm(),
        telegram=telegram,
        store=store,
        renderer=lambda *args, **kwargs: f"{first_part}\n\n{second_part}",
    )

    result = await reporter.run(now=NOW)

    pending = store.pending_deliveries(result.run_id)
    assert result.status == "delivery_pending"
    assert telegram.sent == [first_part]
    assert [(part.part_index, part.attempts) for part in pending] == [(0, 1), (1, 0)]


@pytest.mark.asyncio
async def test_daily_batch_enqueue_failure_leaves_no_partial_parts(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_second_daily_part "
            "BEFORE INSERT ON delivery_parts WHEN NEW.part_index = 1 "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    first_part = "a" * 3_000
    second_part = "b" * 3_000
    reporter = DailyReporter(
        collector=FakeCollector(store, [_candidate("growth/repo", 100)]),
        llm=FakeLlm(),
        telegram=FakeTelegram(),
        store=store,
        renderer=lambda *args, **kwargs: f"{first_part}\n\n{second_part}",
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        await reporter.run(now=NOW)

    with sqlite3.connect(store.path) as connection:
        artifacts = connection.execute("SELECT 1 FROM report_artifacts").fetchall()
    assert artifacts == []
    assert store.pending_deliveries() == []
