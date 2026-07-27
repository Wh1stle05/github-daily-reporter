#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="${GITHUB_DAILY_REPORTER_HOME:-$HOME/workspace/github-daily-reporter}"
cd "$PROJECT_ROOT"
exec .venv/bin/python -m github_daily_reporter.cli daily --config config/reporter.yaml
