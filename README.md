# GitHub Daily Reporter

GitHub Daily Reporter collects promising repositories from GitHub Trending,
GitHub Search, and Hacker News, ranks them deterministically, asks one LLM for
structured Chinese quality review, and sends the daily report to Telegram.
Hermes only schedules the trusted Python job; it does not run an agent or
deliver report text itself.

## Deployment

The operator supplies real credentials and delivery values through environment
variables. Never commit a real token or Telegram destination. Set these values
in `.env`:

```dotenv
GITHUB_TOKEN=...
LLM_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# Optional Telegram forum topic ID
TELEGRAM_MESSAGE_THREAD_ID=
```

```bash
cd "$HOME/workspace/github-daily-reporter"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/github-daily-reporter doctor --config config/reporter.yaml

install -m 700 deploy/hermes/github-daily-run.sh "$HOME/.hermes/scripts/github-daily-run.sh"
hermes cron create '0 9 * * *' '' \
  --name github-daily-reporter \
  --script github-daily-run.sh \
  --no-agent \
  --workdir "$HOME/workspace/github-daily-reporter"

hermes cron status
hermes cron run github-daily-reporter
hermes cron runs github-daily-reporter
```

Before leaving the job enabled, inspect the cron job and confirm its
`next_run_at` resolves to 09:00 in the timezone set in `config/reporter.yaml`.
Run `doctor` after every configuration or token change; it emits one sanitized
JSON object and succeeds only when GitHub, LLM, Telegram, SQLite, Hermes,
timezone, and deployment assets are healthy. Do not add `--deliver`: Python
calls the Telegram Bot API directly, so Hermes delivery would duplicate messages.

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

The wrapper writes one concise operational JSON object to Hermes cron run
output; inspect it with `hermes cron runs github-daily-reporter`. Gateway
service logs remain in the Hermes service manager's journal, while reporter
state, source/review/ranking artifacts, rendered Markdown, and pending delivery
parts live under `data/` and `data/runs/`.

Each report contains two exclusive lists: `成长项目榜` for repositories with
1 to 9,999 stars and `万星增量榜` for repositories with 10,000 or more stars.
An older repository that becomes active again remains eligible. Source
degradation is explicit: a failed Trending, GitHub Search, or Hacker News
source yields a partial report with a data note when other discovery sources
succeed. If collection or the single LLM review fails, Python sends a sanitized
Telegram alert and does not publish an unreviewed trend report. Telegram
transport failures leave durable pending parts; the next daily run retries
those parts before collecting new data.

To rotate the GitHub token, replace only `GITHUB_TOKEN` in `.env`, run
`doctor`, then run one manual daily report before resuming a paused job. Use a
read-only token suitable for public repository metadata. Do not print, paste,
or include the token in logs, reports, cron prompts, or commits.

### Deployment verification (2026-07-24)

Credentialed VPS verification has not been performed from this checkout. It is
an operator prerequisite before enabling delivery: with valid GitHub, LLM, and
Telegram credentials, run `doctor`, one `daily`, and the Hermes manual cron
commands shown above. Record the actual Hermes job ID, execution status,
source-health summary, Telegram message ID, and next-run timestamp only after
that live check succeeds. Do not substitute fixture or local-test results for
deployment evidence.
