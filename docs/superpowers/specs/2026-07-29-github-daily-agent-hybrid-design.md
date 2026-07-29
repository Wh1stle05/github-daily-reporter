# GitHub Daily Reporter: Agent Hybrid Design

## Status

设计已确认，下一步进入实现计划阶段。本文件只定义方案，不改变现有代码、Skill 或部署配置。

## Goals

每日生成两个 GitHub 中文榜单，并按顺序发送到 Telegram：

- 成长项目榜：当前 `1-9,999` Stars，10 个项目。
- 万星增量榜：当前 `>=10,000` Stars，10 个项目。

Python 负责可重复的数据工作；Hermes Agent 负责语义判断、必要核验和中文写作。Agent 可以调整 Python 预排名，但不能修改 Python 评分或加入预选池以外的仓库。

## Non-goals

- 不恢复 `quality-review.json`、`rank`、Schema 校验和自动修复链。
- 不让 Cron Agent 自由控制完整流水线。
- 不要求报告输出严格 JSON 或固定字段 Schema。
- 不把用户技术栈写入 Python 评分公式。
- 不在本阶段增加 Reddit、Product Hunt、X 等新发现来源。

## Architecture

```text
Hermes Cron (--no-agent)
  -> github-daily-run.sh
     -> Python collection/snapshot/scoring
     -> editorial-input.json + evidence/
     -> timeout 900s hermes -z + github-daily-reporter Skill
        -> read candidate index
        -> optionally read evidence or use web_fetch
        -> select and reorder 10 candidates per cohort
        -> write two Markdown files
     -> Python validate and Telegram delivery
     -> run-status.json
```

`--no-agent` applies to the Cron job itself. The wrapper intentionally starts one independent `hermes -z` session for the editorial phase. This keeps collection and delivery outside the Agent loop while preserving Agent judgment for quality and writing.

The outer wrapper owns the 15-minute wall-clock timeout. A timeout, non-zero exit, missing output, or invalid candidate link is a failed run; the wrapper does not ask Hermes to repair the result.

## Discovery

The initial sources remain:

1. GitHub Trending for daily trend signals and `stars today` proxies.
2. GitHub Search API for broader, partitioned discovery across Star and activity ranges.
3. Hacker News Algolia for external discussion signals.

The collector merges and de-duplicates by canonical `owner/repository`, then fetches GitHub repository metadata. Archived, disabled, and invalid repositories are excluded before scoring. Forks are excluded unless an explicit future policy changes this.

The collector should gather roughly 150-300 merged candidates, then produce 20 primary preselected candidates for each cohort. It may also retain up to five same-cohort reserve candidates for replacement when an Agent excludes a primary candidate as an obvious non-project. Search queries must be partitioned so one query does not dominate the pool. GitHub Search `incomplete_results` and source failures are recorded as source health, not silently treated as complete data. If a degraded source leaves fewer than 10 eligible candidates in a cohort after reserves are exhausted, the run is partial and the report contains the available count rather than inventing candidates.

## Snapshots and velocity

The collector stores a daily Star snapshot with its observation timestamp. Velocity precedence is:

1. Difference between the current and previous local snapshots.
2. Trending `stars today` as a proxy, explicitly marked as estimated.
3. Missing value, marked unavailable without inventing a number.

The metadata must retain velocity provenance: measured snapshot, estimated API value, Trending proxy, or unknown. A snapshot delta is converted to a 24-hour rate using the elapsed hours between observations. If the previous observation is missing, duplicated, in the future, or older than 48 hours, the rate is unavailable unless a valid proxy exists. Negative deltas caused by reset or inconsistent data are unavailable, not negative momentum. Existing local snapshot storage may be reused where its semantics match this contract.

## Deterministic scoring

Python computes one `final` score from 0 to 100 and a component breakdown for every candidate. Hermes only reads and displays this score; it never changes it.

### Growth cohort (`1-9,999` Stars)

| Component | Weight |
| --- | ---: |
| Absolute velocity | 35% |
| Relative velocity | 20% |
| Discovery evidence | 20% |
| Activity | 15% |
| Hacker News | 5% |
| Popularity | 5% |

### Mature cohort (`>=10,000` Stars)

| Component | Weight |
| --- | ---: |
| Absolute velocity | 50% |
| Relative velocity | 20% |
| Discovery evidence | 10% |
| Activity | 10% |
| Hacker News | 5% |
| Popularity | 5% |

Every score component is normalized to `[0,100]`, weighted, summed, and rounded to one decimal only after the final sum. Absolute velocity is `clamp(100 * log1p(rate) / log1p(1000), 0, 100)`, where `rate` is non-negative Stars per 24 hours. Measured values receive full weight, snapshot/API estimates receive a 0.90 multiplier, and Trending proxies receive a 0.80 multiplier. Missing velocity contributes zero and retains an `unknown` source marker.

Relative velocity is `clamp(100 * daily_ratio / 0.10, 0, 100)` for the growth cohort and `clamp(100 * daily_ratio / 0.01, 0, 100)` for the mature cohort. `daily_ratio` is the elapsed-time-normalized Star delta divided by the previous Star count, with a minimum denominator of 30. This fixes the existing unit mismatch where a mature repository's decimal rate such as `0.01` was treated as only one point despite its intended 20% weight.

Activity uses a piecewise decay over the most recent meaningful timestamp among creation, push, and release: 100 for <=7 days, 75 for <=30 days, 50 for <=90 days, 25 for <=180 days, otherwise 0. An old repository that becomes active again remains eligible and can score well. Evidence, HN, and popularity retain explicit normalized functions and zero behavior for missing signals; the implementation plan must preserve these functions under a versioned scoring identifier.

Discovery evidence is `clamp(source_confirmation + source_rank_bonus, 0, 100)`, where source confirmation is `min(20 * max(source_count - 1, 0), 40)`, Trending rank contributes 25/15/8 for ranks 1-5/6-15/16+, and Search rank <=20 contributes 10. HN does not add a fixed bonus here. HN score is `clamp(100 * (0.60 * unit(log1p(points), log1p(200)) + 0.40 * unit(log1p(comments), log1p(100))), 0, 100)`. Popularity is `clamp(100 * unit(log1p(total_stars), log1p(50000)), 0, 100)`, with `unit` clamping a ratio to `[0,1]`. HN and popularity are zero when their signals are absent.

The displayed value is one decimal place:

```text
- 综合评分：82.4/100
```

The full breakdown and scoring version remain in JSON artifacts.

## Evidence package

Each run uses a date-specific directory:

```text
data/runs/github-daily-report-YYYY-MM-DD/
├── collection.json
├── editorial-input.json
├── evidence/
├── growth-report.md
├── mature-report.md
└── run-status.json
```

Each invocation has an `attempt_id` and writes Agent outputs under `attempts/<attempt_id>/` first. Canonical reports are replaced atomically only after validation succeeds. The wrapper removes or quarantines stale canonical reports before an attempt, so a timeout cannot cause an older report to be published.

`editorial-input.json` contains a compact index of 40 primary candidates plus up to five same-cohort reserve candidates. Each entry includes the repository URL, Python rank and score, score components, Star and velocity signals, provenance, language, topics, license, creation and activity timestamps, discovery sources, description, a cleaned README opening excerpt, and repository risk markers.

Full README text is not placed into the main index. Detail files under `evidence/` contain cleaned README sections, installation or usage excerpts, directory summaries, release information, and noteworthy anomalies. The index is the default context; detail files are read only when an entry is uncertain or close to the selection boundary.

## Agent responsibilities

The Skill prompt instructs Hermes to:

1. Read `editorial-input.json` before making selections.
2. Prefer project substance, completeness, and verifiable usefulness.
3. Exclude obvious empty shells, promotional pages, resource lists, Awesome lists, learning roadmaps, interview-question collections, course notes, and tutorial indexes that have no independent implementation.
4. Keep real libraries, tools, applications, models, datasets, and complete research prototypes eligible.
5. Select and reorder exactly 10 candidates from each 20-candidate primary cohort, using same-cohort reserves only to fill an obvious exclusion. If fewer than 10 eligible candidates remain after source degradation and reserves are exhausted, write a partial report with the available count and mark the run partial.
6. Use long-term memory only as a weak preference when quality is comparable; do not filter strongly by the user's technology stack.
7. Read detail evidence or use `web_fetch` when the README summary is insufficient or contradictory.
8. Treat fetch failures as non-blocking and do not repeatedly retry the same failed verification.
9. Write `growth-report.md` and `mature-report.md`, then stop.

The Skill must not run collection, ranking commands, schema repair, Telegram delivery, or arbitrary generated scripts. It must not add a repository outside the 40-entry input pool. No numeric Web Fetch budget is imposed; the outer 15-minute timeout and run failure handling are the safety boundary.

## Report format

Both reports use the same readable Markdown-style plain text format. The Telegram sender does not set `parse_mode`, avoiding failures caused by repository names and descriptions containing Markdown characters.

```markdown
# GitHub 成长项目榜 · YYYY-MM-DD

### 1. owner/repo
https://github.com/owner/repo

一句话说明项目解决的问题。

- 综合评分：82.4/100
- 信号：1,286 Stars；今日 +143；TypeScript；GitHub Trending
- 看点：为什么值得关注，实际完成度如何
```

Each complete report has exactly 10 entries and at most two short observation bullets. A partial report contains the available eligible count and is marked partial in run status. The target length is 3,200-3,500 characters, but only the 4,096-character Telegram limit is hard; shorter reports are valid. The report must mark estimated or unavailable velocity in the relevant signal line, without adding a global explanatory paragraph at the top.

## Result validation

Python performs only a bounded, non-repairing check:

- Both files exist and are non-empty.
- Each file has the expected dated title and exactly 10 URLs for a complete run, or the available eligible count for a partial run.
- URLs belong to the correct primary or reserve cohort input.
- There is no cross-cohort duplicate.
- Every selected URL has exactly one `综合评分：N/100` line whose numeric value matches the canonical Python score in `editorial-input.json`.
- The selected URL, title, numbering, and score mapping are unique; no stale output from a previous attempt is accepted.
- UTF-16 message length is within the Telegram limit.

If validation fails, the wrapper marks the run failed and sends an error notification. It does not return the output to Hermes for correction.

## Legacy migration boundary

The implementation must explicitly replace the current single-report and quality-review path. `daily.py`, `reporter.py`, the `rank` CLI command, and the old Hermes Skill are not allowed to remain active entry points for the new Cron job. `CollectionEnvelope.quality_review_path` and legacy `QualityReview` fields must either be removed from the new handoff models or isolated in a compatibility adapter that is not used by the new path. Existing collector, GitHub client, snapshot, and Telegram transport modules should be reused where their contracts match this design. The existing `deploy/hermes/github-daily-run.sh` and any legacy runner must point to the new wrapper, and stale `data/reports/YYYY-MM-DD` output must not be read by the new path.

## Delivery

Telegram delivery is sequential and at-least-once, with best-effort duplicate avoidance:

1. Send the growth report and persist its `message_id`.
2. Send the mature report and persist its `message_id`.
3. Mark the run delivered only after both succeed.

Each part receives up to three attempts with short backoff. Network errors, timeouts, HTTP 5xx, and HTTP 429 are retried; other 4xx errors are recorded without retry. A durable part claim and content digest prevent ordinary reruns from sending a successful first part again. Telegram has no idempotency key, so a crash after server acceptance but before local `message_id` persistence can still duplicate a message; this residual risk is recorded rather than described as strict idempotency.

Pending messages are stored in `run-status.json` and retried before a later collection run. A same-day successful run is not resent unless an explicit force-delivery operation is used. If Telegram is unreachable, the report files remain durable and the failure is recorded locally.

## Failure handling

The wrapper sends a concise Telegram error notification for collection, Hermes, validation, or delivery failures. The notification includes the date, failed stage, reason, source health summary, and run directory. Error notifications have a separate pending state and digest from report parts; if Telegram is unavailable, the error is recorded locally and retried once on the next run, without recursively generating more error notifications.

The outer Hermes process is terminated after 900 seconds as an independent process group: send TERM to the group, wait briefly, then send KILL to remaining descendants, and verify the group exited. The wrapper cleanup trap releases the run lock and records the termination reason. Partial Markdown is never published. Collection JSON and logs remain available for diagnosis.

## Testing and acceptance

Fixture tests must cover cohort boundaries, snapshot velocity, missing data, proxy discounts, deterministic score ordering, input generation, duplicate removal, and UTF-16 length checking.

Integration tests must cover source degradation, insufficient cohorts, multi-day snapshots, LLM timeout, Hermes timeout and descendant cleanup, missing output, stale attempt isolation, out-of-pool URLs, score tampering, first-message success followed by second-message failure, Telegram retries, pending delivery, and same-day duplicate avoidance.

The Skill must be tested in a fresh Hermes session. A live smoke run must confirm both reports are written, contain the expected complete or partial in-pool project counts, display Python scores unchanged, and arrive in the correct Telegram order. The first live Cron run must be manually inspected before enabling the recurring schedule.

## Operational notes

The existing Hermes installation currently uses DDGS for both search and extraction. DDGS is search-only, so Web Fetch requires a later provider configuration change or another supported extraction path. This is a deployment prerequisite for optional verification, but it does not change the architecture or the deterministic collection path.

Hermes session records, Cron run records, JSON artifacts, Markdown reports, and Telegram delivery status are intentionally retained for auditability. Each `hermes -z` editorial invocation is an independent session and does not automatically continue the previous day's conversation, while configured long-term memory remains available.
