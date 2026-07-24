# GitHub Daily Reporter

GitHub Daily Reporter collects promising repositories from GitHub Trending,
GitHub Search, and Hacker News, ranks them deterministically, and lets Hermes
deliver a Chinese daily briefing at 09:00 in the configured IANA timezone.

## Deployment

The operator supplies real credentials and delivery values through environment
variables. Never commit a real GitHub token or Telegram destination. Set a
read-only `GITHUB_TOKEN` in `.env`, and set `TELEGRAM_DELIVER_TARGET` in the
environment used to create the Hermes cron job.

```bash
cd "$HOME/workspace/github-daily-reporter"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/github-daily-reporter doctor --config config/reporter.yaml

install -m 700 deploy/hermes/github-daily-collect.sh "$HOME/.hermes/scripts/github-daily-collect.sh"
mkdir -p "$HOME/.hermes/skills/github-daily-editor"
install -m 600 deploy/hermes/skills/github-daily-editor/SKILL.md "$HOME/.hermes/skills/github-daily-editor/SKILL.md"

test -n "$TELEGRAM_DELIVER_TARGET"
hermes cron create '0 9 * * *' \
  'Use the injected collection JSON and the github-daily-editor skill to produce the final report.' \
  --name github-daily-reporter \
  --script github-daily-collect.sh \
  --skill github-daily-editor \
  --workdir "$HOME/workspace/github-daily-reporter" \
  --deliver "$TELEGRAM_DELIVER_TARGET"

hermes cron status
hermes cron run github-daily-reporter
hermes cron runs github-daily-reporter
```

Before leaving the job enabled, inspect the cron job and confirm its
`next_run_at` resolves to 09:00 in the timezone set in `config/reporter.yaml`.
Run `doctor` after every configuration or token change; it emits one sanitized
JSON object and succeeds only when the token, SQLite state database, GitHub
budget, Hermes scheduler, timezone, and deployment sources are healthy.

## Operations

Pause, resume, or remove the scheduled job with:

```bash
hermes cron pause github-daily-reporter
hermes cron resume github-daily-reporter
hermes cron remove github-daily-reporter
```

Back up SQLite while the reporter is idle (the `.backup` command produces a
consistent copy):

```bash
sqlite3 data/reporter.sqlite3 ".backup data/reporter-$(date +%F).sqlite3"
```

The collection wrapper writes its structured JSON to Hermes cron run output;
inspect it with `hermes cron runs github-daily-reporter`. Gateway service logs
remain in the Hermes service manager's journal, while reporter state and
per-run quality-review files live under `data/` and `data/runs/`.

Source degradation is explicit: a failed Trending, GitHub Search, or Hacker
News source yields a partial report with a data note when other discovery
sources succeed. If all discovery sources fail, no trend list is made; Hermes
delivers an operational failure alert. Missing velocity data similarly remains
labelled unavailable or estimated rather than being inferred.

To rotate the GitHub token, replace only `GITHUB_TOKEN` in `.env`, run
`doctor`, then run one manual collection before resuming a paused job. Use a
read-only token suitable for public repository metadata. Do not print, paste,
or include the token in logs, reports, cron prompts, or commits.

### Deployment verification (2026-07-24)

Credentialed VPS verification has not been performed from this checkout. It is
an operator prerequisite before enabling delivery: with a read-only GitHub
token and real `TELEGRAM_DELIVER_TARGET`, run `doctor`, one `collect`, and the
Hermes manual cron commands shown above. Record the actual Hermes job ID,
execution status, source-health summary, Telegram message ID (when available),
and next-run timestamp only after that live check succeeds. Do not substitute
fixture or local-test results for deployment evidence.
