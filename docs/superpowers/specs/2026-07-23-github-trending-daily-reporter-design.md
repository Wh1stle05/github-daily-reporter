# GitHub Trending Daily Reporter Design

**Date:** 2026-07-23
**Status:** Ready for review

## 1. Purpose

Build a daily GitHub emerging-project reporter that runs on a user's VPS under
Hermes. At 09:00 in the configured timezone, it collects candidates from four
signals, removes low-quality or duplicate repositories, ranks the remaining
projects, asks an LLM to produce a concise Chinese Markdown report, and lets
Hermes deliver the final response to Telegram.

The report is an emerging-project ranking, not an all-time popularity ranking.
Recent momentum and corroboration across independent sources dominate the
score; total stars are only a weak prior.

## 2. Goals

- Run automatically every day at 09:00 in an explicitly configured IANA
  timezone.
- Collect GitHub Trending, GitHub Search, Show HN submissions containing
  GitHub repositories, and candidate-scoped 24-hour star velocity.
- Continue with a clearly marked partial report when one source fails.
- Produce a deterministic, auditable numeric ranking before editorial LLM
  processing.
- Use the LLM for semantic deduplication, quality assessment, selection, and
  Chinese summarization without allowing it to invent facts.
- Deliver through Hermes cron delivery to a configured Telegram chat or topic.
- Persist observations and run history in SQLite for velocity fallback,
  debugging, and later tuning.

## 3. Non-Goals

- Scanning every public GitHub repository for star velocity. Velocity is
  computed for repositories discovered by the other sources plus a rolling
  14-day tracked set.
- Building a web dashboard or interactive UI.
- Training a custom ranking model.
- Posting to platforms other than Telegram in the first release.
- Using top-level `delegate_task` calls as the critical data-collection barrier.
  Current Hermes makes those calls asynchronous and process/session-bound.

## 4. Chosen Architecture

The production path uses a Hermes cron pre-run script for deterministic data
collection and a focused Hermes skill for editorial reasoning:

```text
Hermes Gateway cron tick
  -> trusted wrapper in ~/.hermes/scripts/
  -> Python collection CLI
       -> collect Trending, Search, and HN concurrently
       -> merge canonical repositories
       -> enrich candidates with GraphQL star velocity concurrently
       -> persist source hits, snapshots, and run status
       -> emit bounded JSON on stdout
  -> Hermes injects script stdout into the cron agent prompt
  -> github-daily-editor skill performs semantic review and writes Chinese Markdown
  -> Hermes scheduler stores and delivers the final response to Telegram
```

### 4.1 Why Python Concurrency Instead of Four `delegate_task` Calls

Hermes documentation states that top-level `delegate_task` calls return a
background handle immediately and post their consolidated result later. They
are useful for reasoning-heavy parallel work but do not provide a synchronous,
durable barrier suitable for a cron run that must deliver one complete report.

HTTP collection, pagination, normalization, and arithmetic are mechanical
operations. Python `asyncio` gives them explicit completion semantics, bounded
timeouts, predictable costs, and straightforward tests. The LLM remains in the
part of the pipeline that needs judgment.

If a future Hermes release adds a documented synchronous and durable cron
delegation barrier, delegation may be evaluated as an alternative. It is not a
dependency of this design.

## 5. Repository Layout

```text
github-daily-reporter/
├── README.md
├── pyproject.toml
├── .env.example
├── config/
│   └── reporter.yaml
├── src/github_daily_reporter/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── pipeline.py
│   ├── normalize.py
│   ├── quality.py
│   ├── scoring.py
│   ├── state.py
│   ├── github_client.py
│   └── collectors/
│       ├── __init__.py
│       ├── trending.py
│       ├── github_search.py
│       ├── hacker_news.py
│       └── star_velocity.py
├── deploy/hermes/
│   ├── github-daily-collect.sh
│   └── skills/github-daily-editor/SKILL.md
├── data/
│   └── .gitkeep
├── tests/
│   ├── fixtures/
│   ├── test_trending.py
│   ├── test_github_search.py
│   ├── test_hacker_news.py
│   ├── test_star_velocity.py
│   ├── test_normalize.py
│   ├── test_quality.py
│   ├── test_scoring.py
│   ├── test_state.py
│   └── test_pipeline.py
└── docs/superpowers/
    ├── specs/
    └── plans/
```

Each module has one responsibility:

- `config.py`: load `.env` secrets and YAML behavior settings; reject invalid
  windows, limits, or paths before network work starts.
- `models.py`: define typed source observations, canonical candidates, source
  health, run envelopes, and LLM payloads.
- `github_client.py`: provide shared authenticated REST/GraphQL transport,
  pagination, retry, and rate-limit handling.
- `collectors/`: isolate source-specific parsing and return observations without
  performing cross-source ranking.
- `normalize.py`: extract GitHub repository identities, resolve redirects or
  transfers, and merge exact duplicates.
- `quality.py`: apply deterministic exclusion and penalty rules.
- `scoring.py`: calculate every numeric subscore and stable tie-break ordering.
- `state.py`: own SQLite schema and transactions.
- `pipeline.py`: orchestrate collection, enrichment, persistence, and bounded
  JSON output.
- `cli.py`: expose `collect`, `rank`, `doctor`, and `backfill-snapshots`
  commands. `rank` accepts one run ID plus a schema-validated quality-review
  file and emits the final deterministic order.

## 6. Configuration and Secrets

Secrets live only in the project `.env`:

```dotenv
GITHUB_TOKEN=github_pat_value
```

Behavior belongs in `config/reporter.yaml`:

```yaml
timezone: Asia/Shanghai
new_repo_lookback_days: 7
hn_lookback_hours: 24
velocity_window_hours: 24
velocity_threshold: 50
tracked_repo_days: 14
max_candidates_per_source: 100
max_velocity_candidates: 200
max_llm_candidates: 40
max_report_items: 10
request_timeout_seconds: 20
collection_timeout_seconds: 180
state_db: data/reporter.sqlite3
```

`timezone` is a deployment-time required value and must be the same IANA name
as Hermes `config.yaml`. The example uses `Asia/Shanghai`; the operator changes
it before enabling the cron job if their desired 09:00 is in another zone.

The Telegram destination is stored in Hermes cron state, not project config.
The operator must choose one of these concrete delivery forms during deployment:

- `telegram` for the configured Telegram home channel;
- `telegram:<chat_id>` for a specific chat;
- `telegram:<chat_id>:<thread_id>` for a specific topic.

## 7. Data Model

### 7.1 SourceObservation

Every collector returns records with a common identity surface and a
source-specific metadata map:

```text
source                 trending | github_search | hacker_news
repository_url         normalized candidate URL before API resolution
owner                  parsed owner
name                   parsed repository name
observed_at            timezone-aware UTC timestamp
source_rank            one-based rank where the source has ordering
source_metadata        validated source-specific values
```

### 7.2 RepositoryCandidate

After GitHub API resolution and merging, each candidate contains:

```text
canonical_name         lower-case owner/name identity
full_name              current GitHub display identity
html_url               canonical repository URL
description            repository description or null
created_at             GitHub timestamp
pushed_at              GitHub timestamp or null
archived               boolean
disabled               boolean
is_fork                boolean
license_spdx           SPDX identifier or null
primary_language       string or null
stars_total            current stargazer count
forks_total            current fork count
open_issues_count      current open issue count
stars_24h              exact GraphQL count or null when unavailable
growth_rate_24h        derived ratio or null
velocity_hit           true only when a known stars_24h is greater than 50
trending_rank          one-based rank or null
trending_stars_today   parsed page value or null
search_rank            one-based rank or null
hn_points              integer, zero when absent
hn_comments            integer, zero when absent
hn_item_ids            unique verified HN IDs
discovery_sources      unique subset of trending, github_search, hacker_news
discovery_source_count length of discovery_sources
quality_evidence       README and repository metadata supplied to the LLM
source_errors          candidate-specific enrichment errors
```

`stars_24h` is the canonical momentum count. `trending_stars_today` is retained
as corroborating evidence but does not add a second copy of the same growth
signal to the score. Star velocity is an enrichment signal, not an independent
discovery source, so it never increases `discovery_source_count`.

### 7.3 SQLite Tables

- `repositories`: stable canonical repository identity and latest metadata.
- `repo_snapshots`: timestamped star/fork/issue counts and derived velocity.
- `source_hits`: raw normalized observations for each run and source.
- `collection_runs`: run timestamps, source statuses, counts, and fatal error.
- `reports`: final report text when it can be captured, plus delivery metadata
  if Hermes exposes it to the integration.

Writes for a run occur in a transaction after source results are normalized.
The run row is created as `running`, then finalized as `success`, `partial`, or
`failed`. A process lock prevents manual and scheduled collection from writing
the same run concurrently.

## 8. Source Integrations

### 8.1 GitHub Trending

Request `https://github.com/trending?since=daily` once per run with a descriptive
user agent. Parse repository identity, rank, description, language, total
stars, forks, and the displayed daily star count from `article.Box-row`.

GitHub does not expose a supported Trending API. The parser therefore uses
saved HTML fixtures and fails visibly when no rows are recognized. A Trending
parser failure marks only that source failed.

### 8.2 GitHub Repository Search

Call `GET https://api.github.com/search/repositories` with:

```text
q=created:>=<UTC_DATE_7_DAYS_AGO> stars:>30 fork:false archived:false
sort=stars
order=desc
per_page=100
```

Use authenticated requests with GitHub's recommended media type and API
version. Paginate only until `max_candidates_per_source` is reached. Record
`total_count` and `incomplete_results` for diagnostics. The design does not
attempt to work around GitHub's 1,000-result search ceiling.

### 8.3 Hacker News

Use Algolia HN Search for time-bounded discovery:

```text
GET https://hn.algolia.com/api/v1/search_by_date
tags=show_hn
numericFilters=created_at_i><UTC_CUTOFF>
hitsPerPage=100
```

Extract repository URLs only from the submission URL and sanitized story text.
Reject profile, issue, pull request, release, file, and organization URLs. For
each retained hit, fetch the official Firebase item endpoint and verify that
the ID, title, URL/text, score, comment count, and deletion/dead state agree
with a live HN record.

If Algolia fails, fall back to official `showstories.json` and fetch those
items. The fallback is labeled degraded because it covers the current Show HN
list rather than a complete 24-hour search.

### 8.4 Star Velocity

The velocity candidate set is the union of today's canonical candidates and
repositories observed during the prior 14 days, capped by
`max_velocity_candidates`. Today's candidates take priority when the cap is
reached.

For each candidate, query `Repository.stargazers` ordered by `STARRED_AT` in
descending order. Count `StargazerEdge.starredAt` timestamps newer than the
rolling UTC cutoff and stop pagination after encountering an older edge.

```text
growth_rate_24h = stars_24h / max(stars_total - stars_24h, 30)
```

A repository is a velocity hit when `stars_24h > 50`. The exact count is still
stored for ranking. If GraphQL is unavailable, use the nearest snapshots that
bracket approximately 24 hours and mark the value as estimated. If neither
method is available, keep `stars_24h` null and assign no momentum points rather
than inventing a zero.

## 9. Normalization and Deterministic Filtering

Repository URLs are normalized by removing `.git`, query strings, fragments,
and non-repository subpaths. The GitHub API response's `full_name` becomes the
canonical identity, which handles transfers and case changes.

The pipeline excludes:

- inaccessible, deleted, archived, or disabled repositories;
- forks without evidence of independent development;
- empty repositories or records that do not resolve to repository content;
- exact mirrors and transferred/renamed duplicates;
- candidates identified by the LLM as obvious spam only when it provides an
  evidence-based exclusion reason from the supplied data.

Missing README, missing license, weak documentation, or stale pushes normally
reduce quality rather than cause automatic deletion. This avoids unfairly
removing very new but real projects.

## 10. Ranking

Each eligible candidate receives a deterministic score from 0 to 100:

```text
final_score =
    0.40 * momentum_score
  + 0.15 * evidence_score
  + 0.10 * freshness_score
  + 0.10 * hn_score
  + 0.20 * quality_score
  + 0.05 * popularity_score
```

All component scores are in the range 0 to 100.

### 10.1 Momentum Score

```text
absolute_velocity = min(log(1 + stars_24h) / log(1 + 1000), 1)
relative_velocity = min(growth_rate_24h, 1)
momentum_score = 100 * (0.70 * absolute_velocity + 0.30 * relative_velocity)
```

When velocity is unknown, `momentum_score` is zero and the payload records that
the value was unavailable. A true observed zero also scores zero but remains
distinguishable in stored data.

### 10.2 Evidence Score

Evidence points are additive and capped at 100:

```text
20 points for each independent discovery source after the first, maximum 40
25 points for Trending rank 1-5
15 points for Trending rank 6-15
 8 points for Trending rank 16 or lower
15 points for a verified direct HN repository submission where points or
   comments are greater than zero
10 points for GitHub Search rank 1-20
```

The HN and Search bonuses may coexist with cross-source points because they
represent strength within a source, while the cross-source term represents
independent corroboration.

### 10.3 Freshness Score

Based on age at run time:

```text
0-7 days:     100
8-30 days:     75
31-90 days:    50
91-180 days:   25
over 180 days:  0
```

### 10.4 Hacker News Score

```text
points_score = min(log(1 + hn_points) / log(1 + 200), 1)
comments_score = min(log(1 + hn_comments) / log(1 + 100), 1)
hn_score = 100 * (0.60 * points_score + 0.40 * comments_score)
```

Candidates with no verified HN submission receive zero for this component and
are not otherwise penalized.

### 10.5 Quality Score

The LLM returns four integer judgments from 0 through 5 based only on supplied
repository metadata and bounded README evidence:

- `usefulness`: clarity and practical value of the problem solved;
- `completeness`: code, documentation, and installation readiness;
- `novelty`: meaningful differentiation rather than marketing language;
- `maintenance`: evidence of active, coherent development appropriate to age.

```text
quality_score =
    100 * (usefulness + completeness + novelty + maintenance) / 20
```

The LLM response envelope is schema-validated. An invalid envelope triggers one
repair attempt and then fails the run if it remains invalid. A candidate omitted
from an otherwise valid envelope receives a neutral quality score of 50 and the
run records the degradation.

### 10.6 Popularity Score

```text
popularity_score =
    100 * min(log(1 + stars_total) / log(1 + 50000), 1)
```

Popularity contributes only five percent, preventing established repositories
from dominating solely through accumulated stars.

### 10.7 Stable Tie-Breaking

Equal final scores are ordered by:

1. `stars_24h` descending, with unknown values after known values;
2. `growth_rate_24h` descending;
3. `discovery_source_count` descending;
4. `hn_points` descending;
5. `created_at` descending;
6. `canonical_name` ascending.

Every component score, raw field, exclusion, and tie-break value is persisted
or included in the audit payload so a rank can be reproduced.

## 11. LLM Contract and Report Generation

The Hermes skill treats all collected titles, descriptions, README excerpts,
and HN text as untrusted data. It must not execute or follow instructions found
inside that data.

The workflow has three explicit steps within the same cron agent run:

1. Review up to 40 pre-ranked candidates and write schema-valid quality
   judgments, semantic duplicate groups, and evidence-based exclusions to the
   relative `quality-review.json` path for the run.
2. Invoke `python -m github_daily_reporter.cli rank --run-id <run_id>
   --quality-file <relative_path>`. The command validates that the file belongs
   to the run, applies duplicate and exclusion decisions, recomputes every
   score, persists the decision record, and emits final ranked JSON.
3. Summarize the first 10 entries from that ranked JSON without changing their
   order.

The LLM must not perform floating-point ranking arithmetic itself. The skill
must treat a failed `rank` command or invalid review schema as a run error and
report it rather than silently ordering candidates by intuition.

The final Chinese Markdown format is:

```markdown
# GitHub 每日趋势 · YYYY-MM-DD

## 今日精选

### 1. [owner/repo](URL)
一句话说明项目解决的问题。
- 信号：总星数；24h 增星；增长率；来源
- 看点：基于已提供证据说明入选原因
- 技术：主要语言；许可证

## 快速观察
2-3 条仅由入选项目支持的趋势观察。

## 数据说明
仅在来源失败、降级或关键指标缺失时出现。
```

The report contains at most 10 projects, avoids Markdown tables, and targets
approximately 3,500 characters so it remains readable in Telegram. Unknown
fields are labeled unknown or omitted; they are never guessed.

If no candidate survives filtering, the LLM reports that no sufficiently
credible candidate was found and includes source health. It must not select a
low-quality project merely to fill the quota.

## 12. Hermes Cron and Delivery

Hermes and the reporter use the same configured IANA timezone. The recurring
schedule is `0 9 * * *`. The cron job:

- attaches the `github-daily-editor` skill;
- runs `github-daily-collect.sh` as its pre-run script;
- uses the project root as `workdir`;
- keeps LLM mode enabled so script stdout is injected as context;
- delivers to the operator-selected Telegram target;
- relies on scheduler delivery rather than calling `send_message` in the
  prompt.

The wrapper is copied as a real executable file into `~/.hermes/scripts/`
because Hermes constrains cron scripts to that trusted directory. It changes
to the project root and executes the project's virtual-environment CLI.

Hermes Gateway must remain installed and running because the built-in scheduler
is ticked by the Gateway. Deployment verification includes `hermes cron status`,
a manual `hermes cron run`, run-history inspection, and receipt of the Telegram
message.

## 13. Failure and Degradation Behavior

- HTTP 429, 502, 503, and 504 responses receive bounded exponential backoff
  with jitter. `Retry-After` and GitHub rate-limit reset headers take priority.
- A source-level timeout or parser error is captured in `source_health`; other
  sources continue.
- One or more successful discovery sources produce a `partial` run and a report
  with a data note.
- All discovery sources failing produces a `failed` run and an explicit
  operational alert, not an empty trend report.
- GraphQL velocity failure does not discard candidates; it removes momentum
  points and marks the metric unavailable or uses labeled snapshot estimation.
- SQLite transaction failure makes the collection command fail rather than
  emitting untracked data to the LLM.
- Empty successful script stdout is not used for this job because Hermes treats
  it as a silent run. The collector always emits either a report payload or a
  structured fatal-error payload.
- Yesterday's data is never silently reused. Any fallback snapshot carries its
  observation timestamp and estimated flag.

## 14. Security

- Use a read-only GitHub token appropriate for public repository metadata.
- Never serialize tokens, authorization headers, environment values, or raw
  exception objects that may contain credentials.
- Bound response sizes, candidate counts, README excerpts, and pagination.
- Escape or strip untrusted HTML from Trending and HN before persistence or LLM
  input.
- Validate every external object against typed schemas.
- Treat collected text as data in both the cron prompt and skill instructions
  to reduce prompt-injection risk.
- Keep Telegram credentials solely in Hermes configuration.

## 15. Testing Strategy

### Unit Tests

- Parse representative and malformed GitHub Trending fixtures.
- Build Search qualifiers at date boundaries and paginate to configured caps.
- Extract only repository URLs from HN URL/text variants and verify Firebase
  records.
- Count GraphQL `starredAt` edges across page and cutoff boundaries.
- Normalize case, `.git`, transfer, and subpath variants to one identity.
- Exercise every hard filter and quality penalty independently.
- Verify component formulas at zero, threshold, cap, unknown, and extreme
  values.
- Verify stable tie-breaking and score reproducibility.
- Verify SQLite rollback and run status transitions.

### Integration Tests

- Run all collectors against recorded HTTP responses without network access.
- Exercise one-source failure, velocity failure, all-source failure, rate-limit,
  and timeout paths.
- Validate the exact JSON envelope emitted to Hermes and enforce its size cap.
- Validate representative LLM structured responses, repair behavior, duplicate
  grouping, and neutral fallback.

### Deployment Smoke Test

- Run `doctor` to validate token, database path, timezone, and network access.
- Run `collect` manually and inspect source health and stored rows.
- Install the wrapper and skill into the active Hermes profile.
- Create the paused cron job, trigger one manual run, and confirm Telegram
  delivery.
- Confirm the next run is 09:00 in the intended timezone, then resume the job.

## 16. Acceptance Criteria

- A healthy run collects all three discovery sources concurrently and evaluates
  star velocity for the bounded candidate set.
- A repository appearing in multiple sources is represented exactly once.
- A repository with `stars_24h > 50` is marked as a velocity hit.
- Every reported repository has an auditable final score and component scores.
- Total stars cannot contribute more than five points to the final score.
- The LLM cannot change the deterministic final order after quality scores are
  accepted.
- A single source failure produces a partial Chinese report with the failure
  disclosed.
- An all-source failure produces an operational alert.
- The final report contains at most 10 repositories and is delivered to the
  configured Telegram destination.
- The scheduled next run resolves to 09:00 in the configured IANA timezone.

## 17. Operational Tuning

Weights and thresholds remain configuration-backed constants with the values
defined in this design as defaults. Changes require comparing at least seven
stored daily runs and documenting why the new ranking better matches the
emerging-project objective. The first release does not automatically learn or
change weights.

Metrics to monitor include source success rate, candidate counts, GitHub API
budget, GraphQL pages, collection duration, LLM repair rate, exclusion reasons,
report length, cron status, and delivery errors.

## 18. Verified References

- Hermes Cron: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Hermes Delegation: https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation
- Hermes Delegation Patterns: https://hermes-agent.nousresearch.com/docs/guides/delegation-patterns
- GitHub Repository Search: https://docs.github.com/en/rest/search/search#search-repositories
- GitHub GraphQL StargazerConnection: https://docs.github.com/en/graphql/reference/objects#stargazerconnection
- Hacker News Firebase API: https://github.com/HackerNews/API
- HN Search powered by Algolia: https://hn.algolia.com/api
