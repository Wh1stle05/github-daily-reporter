from github_daily_reporter.models import RepositoryCandidate


def deterministic_exclusion(candidate: RepositoryCandidate) -> str | None:
    """Return the first deterministic reason a repository cannot be ranked."""
    if candidate.archived:
        return "archived"
    if candidate.disabled:
        return "disabled"
    if candidate.is_empty:
        return "empty_repository"
    if candidate.is_fork and not candidate.has_independent_fork_activity:
        return "non_independent_fork"
    return None
