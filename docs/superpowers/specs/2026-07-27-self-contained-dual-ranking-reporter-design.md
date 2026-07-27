# Self-Contained Dual-Ranking Reporter Design

**Date:** 2026-07-27
**Status:** Approved for planning

## Purpose

Replace the Hermes-agent editorial workflow with one self-contained Python
job. Hermes schedules that job with `--no-agent`; Python collects data, calls
one LLM API for structured quality review and Chinese copy, ranks two
repository cohorts deterministically, persists the complete run, and sends the
final Telegram message itself.

This removes agent iteration limits, `execute_code` dependence, repair loops,
and the fragile file handoff between quality review and `rank`.

## Goals

- Deliver two daily lists: a Growth Projects list for repositories with 1 to
  9,999 stars and a Ten-Thousand-Star Momentum list for repositories with
  10,000 or more stars.
- Treat recently reactivated old projects as valid candidates. No repository
  age limit is applied.
- Preserve deterministic collection, source health, deduplication, exclusion,
  audit records, and SQLite velocity snapshots.
- Retain LLM semantic quality assessment without allowing the model to choose
  unseen candidates, alter factual fields, or control message delivery.
- Save each run's source payload, LLM response, ranked data, Markdown, and
  delivery result under `data/runs/<run_id>/`.
- Send Telegram directly from Python with bounded retry and a persisted
  pending-delivery queue.
- On an LLM failure, timeout, or invalid response, send a sanitized Telegram
  failure alert containing the run ID and source health; do not retry through
  an agent or produce an unreviewed trend report.

## Non-Goals

- Claiming the mature list covers all GitHub repositories. It covers mature
  repositories observed through Trending, Search, HN, and the tracked
  snapshot set.
- Calling any Hermes agent, skill, `execute_code`, or `rank` command in the
  production daily path.
- Sending duplicate messages through both Python and Hermes delivery.
- Letting an LLM invent repository facts, URLs, stars, ordering, or rankings.

## Runtime Architecture

```text
Hermes cron --no-agent
  -> trusted Python wrapper (no Hermes --deliver)
  -> reporter daily
       -> retry pending Telegram deliveries
       -> collect, enrich, exclude, persist
       -> deterministic preselection per cohort
       -> one OpenAI-compatible LLM request
       -> strict Pydantic validation of reviews and Chinese snippets
       -> deterministic final ranking per cohort
       -> Python Markdown renderer
       -> write source/review/ranking/report/delivery artifacts atomically
       -> Telegram Bot API direct delivery with retry
```

The wrapper prints a concise JSON status only for operational logs. Telegram
delivery is performed exclusively by Python, so the Hermes cron is created
with `--no-agent` and without `--deliver`.

## Configuration and Secrets

Secrets remain in `.env` and are never serialized:

```dotenv
GITHUB_TOKEN=...
LLM_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# Optional numeric Telegram forum topic ID
TELEGRAM_MESSAGE_THREAD_ID=
```

YAML adds non-secret behaviour:

```yaml
llm_base_url: https://api.openai.com/v1
llm_model: gpt-4.1-mini
llm_timeout_seconds: 45
growth_review_candidates: 20
mature_review_candidates: 12
growth_report_items: 6
mature_report_items: 4
telegram_timeout_seconds: 15
telegram_max_attempts: 3
telegram_retry_base_seconds: 2
```

The LLM endpoint is OpenAI Chat Completions compatible. The implementation
requests JSON-object mode where supported but treats Pydantic validation as the
actual correctness boundary.

## Candidate Eligibility and Cohorts

All existing deterministic exclusions remain hard exclusions: archived,
disabled, empty, and non-independent forks.

After enrichment, candidates are assigned once using the star count captured
for the run:

- `growth`: `1 <= stars_total < 10_000` and at least one of a known positive
  velocity, Trending placement, verified HN signal, or two discovery sources.
- `mature`: `stars_total >= 10_000`.

The eligibility evidence gate prevents ordinary one-star newly-created
repositories discovered by Search alone from filling the Growth list. A
candidate can never appear in both lists.

## Momentum and Activity

Momentum records its provenance and uses the first usable source in this
order:

1. Exact GraphQL `stars_24h`, multiplier `1.00`.
2. SQLite snapshot estimate, multiplier `0.90`.
3. Trending page `trending_stars_today`, multiplier `0.80`.
4. No usable value, score `0` and provenance `unknown`.

The Trending value remains a proxy; it is not written into `stars_24h` or
represented as an exact 24-hour measurement.

Activity replaces creation-only freshness. It uses the more recent of
`created_at` and `pushed_at`, with the existing 7/30/90/180-day bands. This
allows an old repository that is actively maintained to receive an activity
score while retaining a bounded and auditable rule.

## Scores

The LLM produces a 0-100 normalized quality score and an explicit exclusion
decision. Its quality score is used only after it passes schema and identity
validation.

Growth score:

```text
35% momentum + 20% evidence + 15% quality
+ 15% activity + 10% Hacker News + 5% popularity
```

Mature score:

```text
50% absolute momentum + 20% relative growth
+ 10% evidence + 10% activity + 5% Hacker News + 5% popularity
```

For mature candidates, LLM quality can exclude a candidate but does not alter
the numeric ordering. This prevents subjective model scoring from outweighing
observed momentum among established projects.

Each score stores its momentum provenance and component values. Stable
tie-breaks are cohort-local: final score, known momentum, momentum value,
source count, HN points, activity timestamp, then canonical name.

## LLM Contract

Python sends only a bounded projection of the deterministic preselection:
20 Growth candidates and 12 Mature candidates. The Growth preselection
contains the highest deterministic scores plus a fixed number of otherwise
excluded high-evidence/HN candidates, so an evidence-rich project with weak
velocity can still receive review.

The response must include exactly one review per supplied candidate:

```json
{
  "reviews": [
    {
      "canonical_name": "owner/repo",
      "quality_score": 0,
      "exclude": false,
      "exclude_reason": null,
      "summary_zh": "...",
      "highlight_zh": "..."
    }
  ]
}
```

`canonical_name` must match the supplied set exactly. Scores are integers from
0 through 100. `exclude_reason` is required when excluded. Chinese snippets
have fixed character limits and are treated as untrusted display text. Python
provides all URLs, metrics, labels, source names, and ordering.

An HTTP error, timeout, invalid JSON, invalid schema, missing review, extra
review, duplicate identity, or bad identity is an LLM failure. Python writes
the sanitized error artifact and sends an operational alert rather than making
a repair request or falling back to an unreviewed trend list.

## Rendering and Delivery

Python renders, rather than asks the LLM to render, this Markdown shape:

```markdown
# GitHub 每日趋势 · YYYY-MM-DD

## 成长项目榜
### 1. [owner/repo](URL)
一句话介绍
- 信号：...
- 看点：...
- 技术：...

## 万星增量榜
...

## 数据说明
...
```

The renderer emits no more than six Growth entries and four Mature entries,
uses only persisted facts plus bounded reviewed snippets, and includes data
notes for degraded sources, unknown or estimated momentum, and partial runs.
Telegram text is split on Markdown-safe entry boundaries below the service
limit.

Telegram delivery uses Bot API `sendMessage`, a timeout, three attempts with
exponential backoff, and an idempotency key derived from `run_id` and message
part index. A final failure is recorded in SQLite as pending. The next daily
run attempts pending deliveries before starting collection. A run is never
sent through Hermes delivery, which prevents duplicate messages.

## Failure Behaviour

- All discovery sources fail: save a failed run and send a source-health
  alert; do not call the LLM.
- Partial collection: continue, label degraded sources, then continue through
  review and delivery.
- LLM failure: save collection and sanitized LLM error; send a failure alert
  with run ID and source health.
- Telegram failure: persist the final Markdown and delivery attempts; queue it
  for the next run. A local error status is logged because Telegram cannot
  receive an alert while it is unavailable.

## Deployment

Install a wrapper below `~/.hermes/scripts/` that executes the daily CLI.
Create the schedule as:

```bash
hermes cron create '0 9 * * *' '' \
  --name github-daily-reporter \
  --script github-daily-run.sh \
  --no-agent \
  --workdir "$HOME/workspace/github-daily-reporter"
```

The job has no Hermes skill and no `--deliver` option. A doctor command checks
the three secret groups, the LLM endpoint configuration without making a
generation request, Telegram configuration, source assets, and Hermes timezone
before deployment.
