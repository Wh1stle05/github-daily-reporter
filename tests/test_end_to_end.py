import pytest


@pytest.mark.asyncio
async def test_healthy_collection_to_rank_is_reproducible(e2e_harness):
    first = await e2e_harness.collect_and_rank()
    second = await e2e_harness.rank_existing_run(first.run_id)
    assert first.ranked == second.ranked
    assert len(first.ranked) <= 10
    assert all(item.score.popularity <= 100 for item in first.ranked)


@pytest.mark.asyncio
async def test_duplicate_across_three_sources_is_one_candidate(e2e_harness):
    result = await e2e_harness.collect()
    matches = [candidate for candidate in result.candidates if candidate.canonical_name == "acme/tool"]
    assert len(matches) == 1
    assert matches[0].discovery_source_count == 3


@pytest.mark.asyncio
async def test_velocity_failure_preserves_candidate_and_discloses_metric(e2e_harness):
    result = await e2e_harness.collect(graphql_failure=True)
    item = next(candidate for candidate in result.candidates if candidate.canonical_name == "acme/tool")
    assert item.stars_24h is None
    assert item.source_errors


@pytest.mark.asyncio
async def test_sequential_collection_replaces_recorded_routes(e2e_harness, monkeypatch):
    clear_calls = 0
    clear = e2e_harness.respx_mock.clear

    def track_clear():
        nonlocal clear_calls
        clear_calls += 1
        clear()

    monkeypatch.setattr(e2e_harness.respx_mock, "clear", track_clear)
    partial = await e2e_harness.collect(failing_sources={"trending"})
    routes_after_partial = len(e2e_harness.respx_mock.routes)
    failed = await e2e_harness.collect(
        failing_sources={"trending", "github_search", "hacker_news"}
    )
    assert partial.status == "partial"
    assert failed.status == "failed"
    assert failed.fatal_error == "all discovery sources failed"
    assert len(e2e_harness.respx_mock.routes) == routes_after_partial
    assert clear_calls == 2
