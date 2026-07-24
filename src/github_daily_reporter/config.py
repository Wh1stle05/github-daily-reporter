import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, model_validator


class ReporterConfig(BaseModel):
    timezone: str
    github_token: SecretStr
    project_root: Path | None = Field(default=None, exclude=True)
    new_repo_lookback_days: int = Field(default=7, ge=1, le=30)
    hn_lookback_hours: int = Field(default=24, ge=1, le=168)
    velocity_window_hours: int = Field(default=24, ge=1, le=168)
    velocity_threshold: int = Field(default=50, ge=0)
    tracked_repo_days: int = Field(default=14, ge=1, le=90)
    max_candidates_per_source: int = Field(default=100, ge=1, le=1000)
    max_velocity_candidates: int = Field(default=200, ge=1, le=1000)
    max_llm_candidates: int = Field(default=40, ge=1, le=100)
    max_report_items: int = Field(default=10, ge=1, le=20)
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)
    collection_timeout_seconds: float = Field(default=180, gt=0, le=900)
    state_db: Path = Path("data/reporter.sqlite3")

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "ReporterConfig":
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        if self.max_report_items > self.max_llm_candidates:
            raise ValueError("max_report_items cannot exceed max_llm_candidates")
        return self


def _project_root(config_path: Path) -> Path:
    for directory in (config_path.parent, *config_path.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return config_path.parent


def load_config(path: Path) -> ReporterConfig:
    config_path = path.expanduser().resolve()
    project_root = _project_root(config_path)
    load_dotenv(project_root / ".env")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["github_token"] = os.environ.get("GITHUB_TOKEN", "")
    raw["state_db"] = (
        project_root / raw.get("state_db", "data/reporter.sqlite3")
    ).resolve()
    raw["project_root"] = project_root
    return ReporterConfig.model_validate(raw)
