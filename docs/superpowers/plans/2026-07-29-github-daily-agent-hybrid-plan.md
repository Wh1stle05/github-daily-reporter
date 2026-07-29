# GitHub Daily Agent Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy quality-review/report pipeline with deterministic Python collection and scoring followed by one bounded Hermes Skill editorial stage and two Telegram deliveries.

**Architecture:** A no-agent Hermes Cron runs a fixed wrapper. Python collects sources, records timestamped snapshots, scores candidates, and writes a compact handoff plus evidence files. The wrapper starts one independent `hermes -z` process in a 900-second process group; the Skill may inspect evidence or use Web Fetch, writes two Markdown reports, and exits. Python validates immutable score facts and delivers reports sequentially with durable state.

**Tech Stack:** Python 3.11, asyncio, httpx, Pydantic 2, SQLite, filelock, pytest/respx, Bash, Hermes Agent Skill, Telegram Bot API.

---

## File Map

- Modify `src/github_daily_reporter/models.py`: add handoff, attempt, score-version, and report-status models; isolate legacy quality fields.
- Modify `src/github_daily_reporter/scoring.py`: implement the two cohort formulas without LLM quality input.
- Modify `src/github_daily_reporter/state.py`: persist timestamped snapshots, attempts, delivery claims, digests, and separate error notifications.
- Modify `src/github_daily_reporter/pipeline.py`: retain source collection, add reserves, and write date-based artifacts.
- Create `src/github_daily_reporter/editorial.py`: serialize the handoff, write evidence files, validate Agent reports, and atomically promote output.
- Create `src/github_daily_reporter/agent_runner.py`: invoke `hermes -z` in a process group and enforce the 900-second timeout.
- Modify `src/github_daily_reporter/telegram.py`: send plain text without `parse_mode` and validate UTF-16 length.
- Modify `src/github_daily_reporter/cli.py`: add the `hybrid` command and retire `rank` from deployment.
- Create `src/github_daily_reporter/hybrid.py`: orchestrate collection, Agent execution, validation, and delivery.
- Modify `deploy/hermes/github-daily-run.sh` and `deploy/scripts/github-daily-runner.sh`: invoke the hybrid command.
- Create `skills/github-daily-reporter/SKILL.md`: repository source for the registered Skill.
- Modify or create focused tests under `tests/` for each boundary below.

## Task 1: Lock Contracts With Failing Tests

**Files:** `tests/test_editorial.py`, `tests/test_hybrid.py`, `tests/test_hermes_assets.py`

- [ ] Add fixtures for two 20-candidate cohorts, up to five reserves, source health, and canonical Python scores.
- [ ] Add failing validation tests for missing score lines, score tampering, stale output, duplicate URLs, wrong cohort URLs, and out-of-pool URLs.
- [ ] Add failing orchestration tests for collection failure, Agent timeout, partial cohorts, and ordered two-part delivery.
- [ ] Add failing asset tests asserting the wrapper uses `cli hybrid`, not `daily`, `reporter.py`, or `rank`, and that the new Skill source exists.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_editorial.py tests/test_hybrid.py tests/test_hermes_assets.py`; expected result is failure because the new contracts are not implemented.

## Task 2: Replace Score and Snapshot Semantics

**Files:** `src/github_daily_reporter/models.py`, `src/github_daily_reporter/scoring.py`, `src/github_daily_reporter/state.py`, `src/github_daily_reporter/collectors/star_velocity.py`, `tests/test_scoring.py`, `tests/test_star_velocity.py`, `tests/test_state.py`

- [ ] Add failing tests for growth weights `35/20/20/15/5/5`, mature weights `50/20/10/10/5/5`, one-decimal final rounding, source discounts, missing values, and the mature `daily_ratio=0.01` case normalizing to 100.
- [ ] Add failing tests for 12-hour and 36-hour snapshot windows, missing or older-than-48-hour snapshots, negative deltas, and the minimum previous-Star denominator of 30.
- [ ] Implement `score_growth_candidate(candidate, now)` and `score_mature_candidate(candidate, now)` with every component in `[0,100]`, final rounding after weighting, and `scoring_version="agent-hybrid-v1"`.
- [ ] Implement `normalize_elapsed_velocity(delta, elapsed_hours)` and persist observation timestamps. Reject future, duplicate, invalid, and negative snapshots; use Trending proxy only when the measured window is unavailable.
- [ ] Implement exact functions from the design: log-compressed absolute velocity, cohort-specific relative caps, piecewise activity decay, source/rank evidence, logarithmic HN, and logarithmic popularity. Preserve missing-signal zero behavior.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_scoring.py tests/test_star_velocity.py tests/test_state.py`; expected result is PASS.
- [ ] Commit: `git add src/github_daily_reporter/models.py src/github_daily_reporter/scoring.py src/github_daily_reporter/state.py src/github_daily_reporter/collectors/star_velocity.py tests/test_scoring.py tests/test_star_velocity.py tests/test_state.py && git commit -m "feat: version deterministic cohort scoring"`.

## Task 3: Build Handoff and Evidence Artifacts

**Files:** `src/github_daily_reporter/editorial.py`, `src/github_daily_reporter/models.py`, `src/github_daily_reporter/pipeline.py`, `tests/test_editorial.py`, `tests/test_pipeline.py`

- [ ] Add failing tests for `data/runs/github-daily-report-YYYY-MM-DD/`, `collection.json`, `editorial-input.json`, `evidence/`, `attempts/<attempt_id>/`, and `run-status.json`.
- [ ] Add failing tests proving the index has 20 primary candidates per cohort, at most five reserves, bounded README excerpts, score provenance, and source health; detailed evidence must be in separate bounded files.
- [ ] Implement `build_editorial_input(candidates, source_health, run_dir)` and `write_editorial_artifacts(envelope, ranked, run_dir, attempt_id)` with deterministic canonical names and no secrets.
- [ ] Add reserve selection after deterministic exclusions. If fewer than ten eligible candidates remain after reserves, mark the run partial and carry the available count into the handoff.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_editorial.py tests/test_pipeline.py`; expected result is PASS.
- [ ] Commit: `git add src/github_daily_reporter/editorial.py src/github_daily_reporter/models.py src/github_daily_reporter/pipeline.py tests/test_editorial.py tests/test_pipeline.py && git commit -m "feat: add bounded editorial handoff artifacts"`.

## Task 4: Create and Register the Hermes Skill

**Files:** `skills/github-daily-reporter/SKILL.md`, `tests/test_hermes_assets.py`

- [ ] Add a RED test for Skill frontmatter and required behavior.
- [ ] Write a concise Skill that reads `editorial-input.json`, prioritizes project substance and quality, treats stack affinity as weak, excludes non-project educational/list repositories, reads detail evidence or uses Web Fetch for uncertainty, preserves Python scores, writes two reports, never sends Telegram, never runs rank/repair commands, and stops after writing files.
- [ ] State that Web Fetch failures are non-blocking and must not trigger repeated retries. Do not impose a numeric fetch budget.
- [ ] Register the source in the active Hermes profile through the supported Skill management path; verify discovery in a fresh session rather than a resumed session.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_hermes_assets.py`; expected result is PASS.
- [ ] Commit: `git add skills/github-daily-reporter/SKILL.md tests/test_hermes_assets.py && git commit -m "feat: add github daily reporter skill"`.

## Task 5: Bound Hermes and Validate Reports

**Files:** `src/github_daily_reporter/agent_runner.py`, `src/github_daily_reporter/editorial.py`, `tests/test_agent_runner.py`, `tests/test_editorial.py`

- [ ] Add failing tests asserting `start_new_session=True`, TERM to the process group, grace wait, KILL fallback, lock cleanup, and recorded timeout status.
- [ ] Add failing tests for stale canonical Markdown, attempt isolation, atomic promotion, partial counts, score tampering, wrong titles, missing numbering, duplicate URLs, and UTF-16 over-limit output.
- [ ] Implement `run_hermes_editorial(run_dir, attempt_id, timeout_seconds=900)` using an independent process group and sanitized captured output. On timeout, terminate and verify all descendants have exited.
- [ ] Implement narrow parsing for repository URLs and `综合评分：N/100`; compare every selected URL's displayed score to the canonical JSON score and reject out-of-pool or duplicate entries.
- [ ] Write Agent output only under `attempts/<attempt_id>/`; replace canonical reports atomically after validation and quarantine stale files before starting an attempt.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_agent_runner.py tests/test_editorial.py`; expected result is PASS.
- [ ] Commit: `git add src/github_daily_reporter/agent_runner.py src/github_daily_reporter/editorial.py tests/test_agent_runner.py tests/test_editorial.py && git commit -m "feat: bound hermes editorial execution"`.
## Task 6: Make Telegram Delivery Plain-Text and At-Least-Once

**Files:** `src/github_daily_reporter/telegram.py`, `src/github_daily_reporter/state.py`, `tests/test_telegram.py`, `tests/test_state.py`

- [ ] Add failing tests asserting no `parse_mode`, acceptance at 4,096 UTF-16 units, rejection above the limit, `retry_after` handling, three transient attempts, durable claims, and separate error-notification state.
- [ ] Change the Telegram payload to plain text and preserve the existing bounded retry categories. Keep growth then mature delivery strictly sequential.
- [ ] Add a content digest keyed by run ID and part index. Use it to avoid ordinary duplicate sends, while documenting the crash window after Telegram accepts a message before `message_id` is persisted as at-least-once residual risk.
- [ ] Store error notifications separately from report parts; a failed error notification must not overwrite report pending state and may be retried once on the next run without recursive alerts.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_telegram.py tests/test_state.py`; expected result is PASS.
- [ ] Commit: `git add src/github_daily_reporter/telegram.py src/github_daily_reporter/state.py tests/test_telegram.py tests/test_state.py && git commit -m "feat: make telegram delivery durable"`.

## Task 7: Add the Hybrid Command and No-Agent Wrapper

**Files:** `src/github_daily_reporter/cli.py`, `src/github_daily_reporter/hybrid.py`, `deploy/hermes/github-daily-run.sh`, `deploy/scripts/github-daily-runner.sh`, `tests/test_hybrid.py`, `tests/test_hermes_assets.py`

- [ ] Add failing tests for collection failure without Agent invocation, Agent timeout without report delivery, successful two-part delivery order, partial cohort status, same-day rerun, and lock contention returning `skipped`.
- [ ] Implement `run_hybrid(config_path, now=None)` to acquire the run lock, collect, write artifacts, invoke the Agent runner, validate/promote reports, deliver pending messages, and print one sanitized operational JSON object.
- [ ] Add the `hybrid` CLI subcommand. The new command must never call `DailyReporter`, `reporter.py`, or the old `rank` flow.
- [ ] Update both wrappers to resolve `GITHUB_DAILY_REPORTER_HOME`, acquire/clean the lock through the hybrid command, and invoke `python -m github_daily_reporter.cli hybrid --config config/reporter.yaml`.
- [ ] Ensure the Hermes wrapper remains executable and contains no legacy Skill name or `--deliver` duplication.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_hybrid.py tests/test_hermes_assets.py`; expected result is PASS.
- [ ] Commit: `git add src/github_daily_reporter/cli.py src/github_daily_reporter/hybrid.py deploy/hermes/github-daily-run.sh deploy/scripts/github-daily-runner.sh tests/test_hybrid.py tests/test_hermes_assets.py && git commit -m "feat: orchestrate hybrid daily reports"`.

## Task 8: Retire the Legacy Active Path and Update Documentation

**Files:** `src/github_daily_reporter/daily.py`, `src/github_daily_reporter/reporter.py`, `src/github_daily_reporter/models.py`, `README.md`, affected tests under `tests/`

- [ ] Change legacy `daily` and `reporter.py` entry points to explicit compatibility errors or clearly non-production commands; confirm deployed wrappers cannot invoke them.
- [ ] Remove the new-path dependency on `quality_review_path`, `QualityReview`, old LLM review envelopes, and Markdown `parse_mode`; isolate compatibility fields until migrated tests no longer require them.
- [ ] Ensure the new path writes only `data/runs/github-daily-report-YYYY-MM-DD/` and never reads stale `data/reports/YYYY-MM-DD` output.
- [ ] Update README operations for no-agent Cron, nested one-shot Agent session, two reports, 15-minute timeout, partial runs, at-least-once delivery, pending recovery, and manual smoke verification.
- [ ] Run `.venv/bin/python -m pytest -q`; expected result is PASS with legacy tests migrated to the hybrid contract or explicitly asserting the compatibility error.
- [ ] Commit: `git add src/github_daily_reporter/daily.py src/github_daily_reporter/reporter.py src/github_daily_reporter/models.py README.md tests && git commit -m "refactor: retire legacy daily report path"`.

## Task 9: Live Smoke Test and Deployment Verification

**Files:** `config/reporter.yaml` only when existing defaults are insufficient; optional `deploy/hermes/install-skill.sh`; `tests/test_end_to_end.py`

- [ ] Run `.venv/bin/python -m pytest -q tests/test_end_to_end.py`; expected result is PASS for two cohort artifacts, immutable scores, partial behavior, and ordered delivery.
- [ ] Register `skills/github-daily-reporter/SKILL.md` in the active Hermes profile and verify discovery in a fresh session.
- [ ] Run `github-daily-reporter doctor` and one manual hybrid execution with real credentials. Record the run directory, source health, Agent session/usage record, report files, Telegram message IDs, and delivery order without logging secrets.
- [ ] Confirm the actual process-group timeout and stale-output isolation using a controlled test before enabling recurrence.
- [ ] Create the Hermes Cron with `--no-agent --script github-daily-run.sh` only after the manual run succeeds. Verify schedule timezone and one manually triggered Cron run.
- [ ] Run `.venv/bin/python -m pytest -q` again before claiming completion; report any live network or Telegram limitation separately from fixture results.

