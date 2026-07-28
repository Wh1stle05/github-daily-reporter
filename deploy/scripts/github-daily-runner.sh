#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${GITHUB_DAILY_REPORTER_HOME:-$HOME/workspace/github-daily-reporter}"
cd "$PROJECT_ROOT"

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
export LLM_API_KEY="${LLM_API_KEY:-}"
export LLM_MODEL="${LLM_MODEL:-deepseek-v4-flash}"
export LLM_BASE_URL="${LLM_BASE_URL:-https://api.deepseek.com}"

exec .venv/bin/python -m github_daily_reporter.reporter --config config/reporter.yaml
