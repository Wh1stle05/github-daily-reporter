# Self-Contained Dual-Ranking Reporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Hermes-agent editorial path with a Python job that produces Growth and Ten-Thousand-Star rankings, makes one schema-validated LLM review call, renders Markdown, persists artifacts, and delivers to Telegram directly.

**Architecture:** Hermes runs one trusted script with `--no-agent` and no `--deliver` option. The Python daily command owns collection, two-cohort selection, LLM review, deterministic ranking, rendering, SQLite artifacts, Telegram retry, and pending-delivery recovery.

**Tech Stack:** Python 3.11+, Pydantic 2, httpx, SQLite, pytest, pytest-asyncio, respx, Hermes cron.

---

## File Map

- Modify: `config/reporter.yaml`, `.env.example`, `src/github_daily_reporter/config.py`, `models.py`, `scoring.py`, `state.py`, `cli.py`, `README.md`.
- Create: `selection.py`, `llm.py`, `render.py`, `telegram.py`, `daily.py`, and their focused test modules.
- Create: `deploy/hermes/github-daily-run.sh`.
- Delete: `deploy/hermes/github-daily-collect.sh` and `deploy/hermes/skills/github-daily-editor/SKILL.md`.

### Task 1: Direct Runtime Configuration and Contracts

**Files:**
- Modify: `src/github_daily_reporter/config.py`, `src/github_daily_reporter/models.py`, `config/reporter.yaml`, `.env.example`
- Test: `tests/test_config.py`, `tests/test_models.py`

- [ ] **Step 1: Write failing config and model tests**

```python
def test_config_loads_direct_runtime_secrets(config_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = load_config(config_path)
    assert config.llm_model == "gpt-4.1-mini"
    assert config.growth_report_items == 6
    assert config.mature_report_items == 4


def test_llm_review_rejects_bad_score_and_oversized_copy():
    with pytest.raises(ValidationError):
        LlmReview.model_validate({"canonical_name": "a/repo", "quality_score": 101,
                                  "exclude": False, "summary_zh": "x", "highlight_zh": "y"})
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_models.py -q`

Expected: failures because the direct-runtime fields and LLM contracts do not exist.

- [ ] **Step 3: Implement the typed surface**

Add validated `ReporterConfig` fields:

```python
llm_base_url: str = "https://api.openai.com/v1"
llm_model: str = "gpt-4.1-mini"
llm_api_key: SecretStr
llm_timeout_seconds: float = Field(default=45, gt=0, le=120)
growth_review_candidates: int = Field(default=20, ge=4, le=40)
mature_review_candidates: int = Field(default=12, ge=2, le=30)
growth_report_items: int = Field(default=6, ge=1, le=10)
mature_report_items: int = Field(default=4, ge=1, le=10)
telegram_bot_token: SecretStr
telegram_chat_id: str
telegram_message_thread_id: int | None = Field(default=None, ge=1)
telegram_timeout_seconds: float = Field(default=15, gt=0, le=60)
telegram_max_attempts: int = Field(default=3, ge=1, le=5)
telegram_retry_base_seconds: float = Field(default=2, gt=0, le=10)
```

Load all secrets from environment only. Reject report-item totals above 10 and review-candidate counts below their report-item count.

Add literals `Cohort = Literal["growth", "mature"]` and `MomentumSource`, plus `LlmReview`, `LlmReviewEnvelope`, `CohortScoreBreakdown`, `DailyRunResult`, and `DeliveryPart`. LLM scores are integers 0..100, excluded reviews require a reason, and Chinese snippets are capped at 160 and 240 characters.

- [ ] **Step 4: Add configuration examples and verify green**

Add only names for `LLM_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` to `.env.example`; add non-secret defaults to YAML.

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_models.py -q`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/github_daily_reporter/config.py src/github_daily_reporter/models.py config/reporter.yaml .env.example tests/test_config.py tests/test_models.py
git commit -m "feat: configure direct report runtime"
```

### Task 2: Dual Cohort Selection and Scores

**Files:**
- Create: `src/github_daily_reporter/selection.py`
- Modify: `src/github_daily_reporter/scoring.py`
- Test: `tests/test_selection.py`, `tests/test_scoring.py`

- [ ] **Step 1: Write failing cohort and fallback tests**

```python
def test_assign_cohort_splits_at_ten_thousand(candidate_factory):
    assert assign_cohort(candidate_factory(stars_total=9_999, stars_24h=1)) == "growth"
    assert assign_cohort(candidate_factory(stars_total=10_000)) == "mature"


def test_growth_rejects_one_star_search_only_candidate(candidate_factory):
    candidate = candidate_factory(stars_total=1, stars_24h=None,
                                  discovery_sources={"github_search"})
    assert assign_cohort(candidate) is None


def test_trending_velocity_is_discounted_proxy(candidate_factory):
    candidate = candidate_factory(stars_24h=None, trending_stars_today=500,
                                  discovery_sources={"trending"})
    score = score_growth_candidate(candidate, NOW, quality_score=50)
    assert score.momentum_source == "trending_proxy"
    assert score.momentum > 0
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_selection.py tests/test_scoring.py -q`

Expected: import failures for selection and cohort score functions.

- [ ] **Step 3: Implement eligibility and preselection**

Implement:

```python
def assign_cohort(candidate: RepositoryCandidate) -> Cohort | None:
    if candidate.stars_total >= 10_000:
        return "mature"
    has_signal = (
        (candidate.stars_24h or 0) > 0
        or candidate.trending_rank is not None
        or candidate.discovery_source_count >= 2
        or ("hacker_news" in candidate.discovery_sources
            and (candidate.hn_points > 0 or candidate.hn_comments > 0))
    )
    return "growth" if candidate.stars_total >= 1 and has_signal else None
```

Implement `select_review_candidates`: take the highest deterministic preliminary scores per cohort, reserving up to four Growth positions for not-yet-selected candidates sorted by evidence, HN points, then canonical name. Cap Growth at 20 and Mature at 12 by config.

- [ ] **Step 4: Implement provenance-aware scoring**

Add `momentum_signal(candidate) -> tuple[float, MomentumSource]`:

- exact GraphQL velocity: multiplier 1.00;
- snapshot estimate: multiplier 0.90;
- Trending daily stars proxy: multiplier 0.80;
- unavailable: 0 and `unknown`.

Never write Trending data into `stars_24h`. Replace creation-only freshness with activity using the later of `created_at` and `pushed_at`, retaining 7/30/90/180-day bands.

Implement:

```python
growth = 0.35 * momentum + 0.20 * evidence + 0.15 * quality + 0.15 * activity + 0.10 * hn + 0.05 * popularity
mature = 0.50 * absolute_momentum + 0.20 * relative_growth + 0.10 * evidence + 0.10 * activity + 0.05 * hn + 0.05 * popularity
```

Mature LLM quality is an exclusion gate only. Rank each cohort by final score, known momentum, momentum value, source count, HN points, activity timestamp, canonical name.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_selection.py tests/test_scoring.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/selection.py src/github_daily_reporter/scoring.py tests/test_selection.py tests/test_scoring.py
git commit -m "feat: add dual cohort ranking"
```

### Task 3: One-Call Structured LLM Review

**Files:**
- Create: `src/github_daily_reporter/llm.py`
- Test: `tests/test_llm.py`

- [ ] **Step 1: Write failing direct API tests**

```python
@pytest.mark.asyncio
async def test_review_posts_one_bounded_request(respx_mock, config, candidate_factory):
    route = respx_mock.post("https://llm.example/v1/chat/completions").respond(
        200, json={"choices": [{"message": {"content": json.dumps({"reviews": []})}}]}
    )
    with pytest.raises(LlmReviewError, match="identity_mismatch"):
        await LlmReviewClient(config).review([candidate_factory(canonical_name="a/repo")])
    assert route.call_count == 1
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_llm.py -q`

Expected: missing LLM client.

- [ ] **Step 3: Implement request and validation**

Create `LlmReviewClient.review(candidates)`. Project only bounded candidate facts: identity, cohort, description, README evidence, metrics, sources, timestamps, and preliminary score. Prompt that all supplied fields are untrusted data and the model must return one JSON review for every supplied identity.

POST once to:

```python
f"{config.llm_base_url.rstrip('/')}/chat/completions"
```

Use bearer auth, temperature 0, configured timeout, and JSON-object response mode. Validate JSON, Pydantic shape, exact identity-set equality, no duplicates, and every exclusion reason. Raise only sanitized categories: `timeout`, `transport`, `http_status`, `invalid_json`, `invalid_schema`, or `identity_mismatch`. Do not implement repair or retry.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_llm.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/llm.py tests/test_llm.py
git commit -m "feat: add one-call LLM review"
```

### Task 4: Persist Artifacts and Pending Deliveries

**Files:**
- Modify: `src/github_daily_reporter/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write failing durable delivery tests**

```python
def test_delivery_part_round_trip(store):
    store.enqueue_delivery("run-1", 0, "message", "digest")
    assert store.pending_deliveries()[0].body == "message"
    store.mark_delivery_delivered("run-1", 0, "42")
    assert store.pending_deliveries() == []
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_state.py -q`

Expected: missing delivery APIs.

- [ ] **Step 3: Add transactional state**

Add a `report_artifacts` table keyed by run ID for source JSON, review JSON, ranking JSON, Markdown, and timestamps. Add a `delivery_parts` table keyed by `(run_id, part_index)` containing message body, SHA-256 digest, attempts, state, Telegram message ID, sanitized error category, and timestamps.

Implement `save_report_artifacts`, `enqueue_delivery`, `pending_deliveries`, `record_delivery_attempt`, `mark_delivery_delivered`, and `mark_delivery_pending`. A same-key same-digest enqueue is idempotent; a changed digest raises `ValueError`.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_state.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/state.py tests/test_state.py
git commit -m "feat: persist report delivery queue"
```

### Task 5: Python Renderer and Telegram Client

**Files:**
- Create: `src/github_daily_reporter/render.py`, `src/github_daily_reporter/telegram.py`
- Test: `tests/test_render.py`, `tests/test_telegram.py`

- [ ] **Step 1: Write failing rendering and retry tests**

```python
def test_render_uses_disjoint_dual_lists(ranked_growth, ranked_mature):
    text = render_report(DATE, ranked_growth, ranked_mature, source_health=[])
    assert "## 成长项目榜" in text
    assert "## 万星增量榜" in text
    assert len(text) <= 3500


@pytest.mark.asyncio
async def test_telegram_retries_503_then_succeeds(respx_mock, config):
    route = respx_mock.post("https://api.telegram.org/botbot/sendMessage")
    route.side_effect = [httpx.Response(503), httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})]
    assert await TelegramClient(config).send("hello") == "42"
    assert route.call_count == 2
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_render.py tests/test_telegram.py -q`

Expected: missing modules.

- [ ] **Step 3: Implement deterministic rendering**

Render at most six Growth and four Mature entries in the supplied order. Python owns URLs, stars, source labels, language, license, and all Markdown structure; LLM copy is limited to validated summary/highlight fields. Include `数据说明` only for degraded sources or selected candidates with unknown/estimated/proxy momentum. Implement `render_failure_alert(run_id, phase, category, source_health)` without raw errors or secrets.

- [ ] **Step 4: Implement direct Telegram transport**

Call Bot API `sendMessage` with chat ID and optional thread ID. Split on full-entry boundaries before 3,800 characters. Retry only timeout/transport, 429, and 5xx responses using `retry_after` or exponential backoff; stop after configured attempts. Return a message ID or a stable terminal error category.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_render.py tests/test_telegram.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/render.py src/github_daily_reporter/telegram.py tests/test_render.py tests/test_telegram.py
git commit -m "feat: render and deliver Telegram reports"
```

### Task 6: Daily Orchestrator

**Files:**
- Create: `src/github_daily_reporter/daily.py`
- Modify: `src/github_daily_reporter/pipeline.py`
- Test: `tests/test_daily.py`

- [ ] **Step 1: Write failing workflow tests**

```python
@pytest.mark.asyncio
async def test_daily_reviews_once_ranks_and_delivers(harness):
    result = await harness.run()
    assert result.status == "delivered"
    assert harness.llm.call_count == 1
    assert len(result.growth) <= 6
    assert len(result.mature) <= 4


@pytest.mark.asyncio
async def test_llm_timeout_sends_sanitized_alert_without_report(harness):
    harness.llm.fail_with("timeout")
    result = await harness.run()
    assert result.status == "llm_failed"
    assert "timeout" in harness.telegram.sent_text
    assert "sk-" not in harness.telegram.sent_text
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_daily.py -q`

Expected: missing daily workflow.

- [ ] **Step 3: Implement ordered, single-call flow**

`DailyReporter.run()` must first resend persisted pending delivery parts, then collect. A failed collection sends only a source-health alert and never calls the LLM. For a successful/partial collection, load all persisted candidates from `StateStore`, assign cohorts, preselect review candidates, and call the LLM exactly once.

Apply LLM exclusions. Use LLM quality only in Growth scoring; Mature quality is exclusion-only. Render, save source/review/ranking/Markdown artifacts, enqueue each Telegram part, and attempt delivery. LLM errors persist a sanitized artifact and send an alert without a repair call. Telegram final failures leave queued parts and return `delivery_pending`.

- [ ] **Step 4: Preserve old diagnostics without using them in production**

Keep `collect` and `rank` commands as manual diagnostics. The daily flow must load full eligible candidates from state rather than the old bounded `CollectionEnvelope.candidates` handoff.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_daily.py tests/test_pipeline.py tests/test_state.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/daily.py src/github_daily_reporter/pipeline.py tests/test_daily.py
git commit -m "feat: run self-contained daily reports"
```

### Task 7: CLI and Hermes No-Agent Deployment

**Files:**
- Modify: `src/github_daily_reporter/cli.py`, `tests/test_cli.py`, `tests/test_hermes_assets.py`
- Create: `deploy/hermes/github-daily-run.sh`
- Delete: `deploy/hermes/github-daily-collect.sh`, `deploy/hermes/skills/github-daily-editor/SKILL.md`

- [ ] **Step 1: Write failing command and wrapper tests**

```python
def test_daily_prints_one_operational_json(monkeypatch, capsys, config_path):
    monkeypatch.setattr("github_daily_reporter.cli.run_daily", fake_daily)
    assert main(["daily", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "delivered"


def test_no_agent_wrapper_runs_daily_only():
    text = (ROOT / "deploy/hermes/github-daily-run.sh").read_text()
    assert "github_daily_reporter.cli daily" in text
    assert "github-daily-editor" not in text
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_hermes_assets.py -q`

Expected: missing daily command and wrapper.

- [ ] **Step 3: Implement daily CLI and doctor checks**

Add `daily --config` and `run_daily(config_path)`; stdout is one concise operational JSON object, never report Markdown. Extend doctor to validate non-empty LLM/Telegram secrets, optional positive thread ID, delivery database access, and the new wrapper. Doctor must not call the LLM or Telegram API.

Create exactly:

```bash
#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${GITHUB_DAILY_REPORTER_HOME:-$HOME/workspace/github-daily-reporter}"
cd "$PROJECT_ROOT"
exec .venv/bin/python -m github_daily_reporter.cli daily --config config/reporter.yaml
```

Set mode 0755. Remove the old wrapper and agent skill so there is one production path.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_hermes_assets.py -q`

Expected: all pass.

```bash
git add src/github_daily_reporter/cli.py tests/test_cli.py tests/test_hermes_assets.py deploy/hermes/github-daily-run.sh
git rm deploy/hermes/github-daily-collect.sh deploy/hermes/skills/github-daily-editor/SKILL.md
git commit -m "feat: run reports without Hermes agents"
```

### Task 8: Migration Documentation and Release Verification

**Files:**
- Modify: `README.md`, `tests/test_end_to_end.py`, `tests/conftest.py`

- [ ] **Step 1: Add a dual-list end-to-end test**

```python
@pytest.mark.asyncio
async def test_daily_report_has_disjoint_cohorts(e2e_daily_harness):
    report = await e2e_daily_harness.run()
    assert "## 成长项目榜" in report.markdown
    assert "## 万星增量榜" in report.markdown
    assert not (set(report.growth_names) & set(report.mature_names))
```

- [ ] **Step 2: Confirm red**

Run: `.venv/bin/python -m pytest tests/test_end_to_end.py -q`

Expected: missing daily harness or report contract.

- [ ] **Step 3: Document no-agent deployment**

Replace skill installation and the agent cron command with:

```bash
install -m 700 deploy/hermes/github-daily-run.sh "$HOME/.hermes/scripts/github-daily-run.sh"
hermes cron create '0 9 * * *' '' \
  --name github-daily-reporter \
  --script github-daily-run.sh \
  --no-agent \
  --workdir "$HOME/workspace/github-daily-reporter"
```

State explicitly that `--deliver` is absent because Python sends Telegram. Document the two cohorts, secrets, failure alerts, retry queue, pause/resume, and live VPS verification.

- [ ] **Step 4: Run complete verification**

Run: `.venv/bin/python -m pytest -q`

Expected: zero failures.

Run: `.venv/bin/python -m compileall -q src tests`

Expected: exit 0.

Run: `uv pip check --python .venv/bin/python`

Expected: all installed packages are compatible.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_end_to_end.py tests/conftest.py
git commit -m "docs: deploy self-contained dual rankings"
```

## Plan Review

### Spec Coverage

- Tasks 1 and 3 cover direct LLM configuration, one-call structured output, exact identity validation, and no repair loop.
- Task 2 covers the 1..9,999 Growth boundary, 10,000+ Mature boundary, old-project reactivation, proxy momentum, estimated discount, and activity scoring.
- Tasks 4 through 6 cover persisted JSON/Markdown, direct Telegram retries, queued recovery, source failures, LLM failures, and delivery failures.
- Task 7 removes the Hermes agent production path and makes `--no-agent` unambiguous.
- Task 8 verifies both lists and replaces the deployment instructions.

### Consistency Checks

- Python is the only component that ranks, renders, saves, and delivers; no task introduces Hermes `--deliver` or a skill.
- The LLM can exclude, score Growth candidates, and provide bounded Chinese copy; it cannot add identities, alter facts, select unseen candidates, or reorder the output.
- Cohorts are assigned from the star count captured in a single run and are mutually exclusive.
- LLM failure emits a sanitized Telegram alert without a fallback report, as requested. Telegram failure is queued because an unavailable Telegram transport cannot receive an alert.

### External Verification Boundary

The final live test needs real GitHub, LLM, and Telegram credentials. No implementation task creates a live cron job or sends a live Telegram message until the operator explicitly authorizes those external effects.
