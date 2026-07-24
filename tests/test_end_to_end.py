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
    result = await e2e_harness.collect(graphql_failure=True, no_snapshot=True)
    item = next(candidate for candidate in result.candidates if candidate.canonical_name == "acme/tool")
    assert item.stars_24h is None
    assert item.source_errors


@pytest.mark.asyncio
async def test_one_source_failure_is_partial_and_all_source_failure_is_failed(e2e_harness):
    partial = await e2e_harness.collect(failing_sources={"trending"})
    failed = await e2e_harness.collect(
        failing_sources={"trending", "github_search", "hacker_news"}
    )
    assert partial.status == "partial"
    assert failed.status == "failed"
    assert failed.fatal_error == "all discovery sources failed"
