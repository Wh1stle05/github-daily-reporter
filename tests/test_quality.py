from github_daily_reporter.quality import deterministic_exclusion


def test_archived_and_empty_repositories_are_excluded(candidate_factory):
    assert deterministic_exclusion(candidate_factory(archived=True)) == "archived"
    assert deterministic_exclusion(candidate_factory(is_empty=True)) == "empty_repository"


def test_exclusion_checks_follow_the_documented_order(candidate_factory):
    candidate = candidate_factory(archived=True, disabled=True, is_empty=True)

    assert deterministic_exclusion(candidate) == "archived"


def test_disabled_and_non_independent_forks_are_excluded(candidate_factory):
    assert deterministic_exclusion(candidate_factory(disabled=True)) == "disabled"
    assert (
        deterministic_exclusion(candidate_factory(is_fork=True))
        == "non_independent_fork"
    )


def test_independent_fork_is_not_automatically_excluded(candidate_factory):
    candidate = candidate_factory(is_fork=True, has_independent_fork_activity=True)

    assert deterministic_exclusion(candidate) is None


def test_missing_metadata_and_stale_push_are_not_deterministic_exclusions(
    candidate_factory,
):
    candidate = candidate_factory(license_spdx=None, pushed_at=None)

    assert deterministic_exclusion(candidate) is None
