#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${GITHUB_DAILY_REPORTER_HOME:-$HOME/workspace/github-daily-reporter}"
cd "$PROJECT_ROOT"

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
exec .venv/bin/python -m github_daily_reporter.cli hybrid --config config/reporter.yaml
