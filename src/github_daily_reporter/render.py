"""Deterministic Markdown rendering for the daily Chinese report."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Iterable
from uuid import UUID


_SOURCE_LABELS = {
    "trending": "GitHub Trending",
    "github_search": "GitHub Search",
    "hacker_news": "Hacker News",
}
_MOMENTUM_LABELS = {
    "exact": "精确 24 小时增量",
    "graphql": "精确 24 小时增量",
    "snapshot_estimate": "快照估算",
    "estimated": "快照估算",
    "trending_proxy": "Trending 代理信号",
    "proxy": "Trending 代理信号",
    "unknown": "未知",
}
_ALERT_PHASES = frozenset({"collection", "llm", "ranking", "render", "delivery", "telegram"})
_ALERT_CATEGORIES = frozenset(
    {
        "timeout",
        "transport",
        "http_status",
        "http_429",
        "http_5xx",
        "rate_limited",
        "server_error",
        "invalid_response",
        "invalid_json",
        "invalid_schema",
        "identity_mismatch",
        "message_entry_too_large",
    }
)
_ALERT_SOURCE_STATES = frozenset({"success", "degraded", "failed"})
_MARKDOWN_ESCAPES = str.maketrans({char: f"\\{char}" for char in r"\\_*[]()~`>#+-=|{}.!"})
_GITHUB_REPOSITORY_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z"
)


def render_report(
    report_date: date | datetime | str,
    ranked_growth: Iterable[Any],
    ranked_mature: Iterable[Any],
    *,
    source_health: Iterable[Any],
    growth_limit: int = 6,
    mature_limit: int = 4,
) -> str:
    """Render supplied rankings in their existing order.

    All factual fields and Markdown structure come from the candidate. Reviewed
    text is used only for the two bounded copy fields.
    """
    day = _date_text(report_date)
    growth = list(ranked_growth)[: min(max(growth_limit, 0), 6)]
    mature = list(ranked_mature)[: min(max(mature_limit, 0), 4)]
    lines = [_escape_markdown(f"# GitHub 每日趋势 · {day}"), "", _escape_markdown("## 成长项目榜"), ""]
    lines.extend(_render_entries(growth))
    lines.extend(["", _escape_markdown("## 万星增量榜"), ""])
    lines.extend(_render_entries(mature))

    notes = _data_notes([*growth, *mature], source_health)
    if notes:
        lines.extend(["", _escape_markdown("## 数据说明"), ""])
        lines.extend(_escape_markdown(f"- {note}") for note in notes)
    return "\n".join(lines).rstrip() + "\n"


def render_failure_alert(
    run_id: str, phase: str, category: str, source_health: Iterable[Any]
) -> str:
    """Create a sanitized operational alert from identifiers and health only."""
    lines = [
        "GitHub 每日趋势任务失败",
        _escape_markdown(f"- run_id：{_safe_run_id(run_id)}"),
        _escape_markdown(f"- 阶段：{_allowlisted(phase, _ALERT_PHASES)}"),
        _escape_markdown(f"- 类别：{_allowlisted(category, _ALERT_CATEGORIES)}"),
    ]
    health = list(source_health)
    if health:
        lines.append(_escape_markdown("- 数据源："))
        for item in health:
            source = _allowlisted(_field(item, "source", "unknown"), _SOURCE_LABELS)
            status = _allowlisted(_field(item, "status", "unknown"), _ALERT_SOURCE_STATES)
            count = _safe_count(_field(item, "item_count", 0))
            lines.append("  " + _escape_markdown(f"- {source}: {status}（{count} 条）"))
    return "\n".join(lines)


def _render_entries(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for index, ranked in enumerate(items, 1):
        candidate = _field(ranked, "candidate", ranked)
        name = _safe_text(_field(candidate, "canonical_name", _field(candidate, "full_name", "unknown/repository")), 200)
        url = _safe_url(_field(candidate, "html_url", ""))
        title = f"[{_escape_markdown(name)}]({_escape_link_url(url)})" if url else _escape_markdown(name)
        review = _field(ranked, "review", _field(ranked, "llm_review", None))
        summary = _copy(review, "summary_zh", "暂无简介")
        highlight = _copy(review, "highlight_zh", "暂无特别看点")
        lines.extend(
            [
                f"\\#\\#\\# {index}\\. {title}",
                _escape_markdown(summary),
                _escape_markdown(f"- 信号：{_signal(candidate, ranked)}"),
                _escape_markdown(f"- 看点：{highlight}"),
                _escape_markdown(f"- 技术：{_technology(candidate)}"),
                "",
            ]
        )
    return lines[:-1] if lines else []


def _signal(candidate: Any, ranked: Any) -> str:
    stars = _field(candidate, "stars_total", 0)
    stars_24h = _field(candidate, "stars_24h", None)
    estimated = bool(_field(candidate, "stars_24h_estimated", False))
    source = _field(_field(ranked, "score", None), "momentum_source", None)
    if source is None:
        source = _field(candidate, "momentum_source", "unknown")
    source_key = str(source)
    label = _MOMENTUM_LABELS.get(source_key, source_key)
    if stars_24h is None:
        proxy = _field(candidate, "trending_stars_today", None)
        velocity = f"Trending 今日 +{proxy}" if proxy is not None else "24 小时增量未知"
    else:
        qualifier = "（估算）" if estimated else ""
        velocity = f"24 小时 +{stars_24h}{qualifier}"
    sources = _field(candidate, "discovery_sources", set()) or set()
    source_names = "、".join(_SOURCE_LABELS.get(str(item), str(item)) for item in sorted(sources))
    return f"{velocity}；{label}" + (f"；来源：{source_names}" if source_names else "") + f"；总星标 {stars}"


def _technology(candidate: Any) -> str:
    language = _field(candidate, "primary_language", None) or "未知语言"
    license_name = _field(candidate, "license_spdx", None) or "许可证未知"
    return f"{_safe_text(language, 80)} · {_safe_text(license_name, 80)}"


def _data_notes(items: list[Any], source_health: Iterable[Any]) -> list[str]:
    notes: list[str] = []
    for item in source_health:
        status = str(_field(item, "status", "success"))
        if status != "success":
            source = str(_field(item, "source", "unknown"))
            notes.append(f"{_SOURCE_LABELS.get(source, source)}（{source}）数据源状态为 {status}，本次结果可能不完整。")
    momentum_notes: set[str] = set()
    for ranked in items:
        candidate = _field(ranked, "candidate", ranked)
        source = _field(_field(ranked, "score", None), "momentum_source", None)
        source = source if source is not None else _field(candidate, "momentum_source", "unknown")
        key = str(source)
        if key in {"snapshot_estimate", "estimated"} or _field(candidate, "stars_24h_estimated", False):
            momentum_notes.add("部分项目的增量来自历史快照估算。")
        elif key in {"trending_proxy", "proxy"}:
            momentum_notes.add("部分项目使用 Trending 今日增量作为代理信号，并非精确 24 小时数据。")
        elif key == "unknown" or (_field(candidate, "stars_24h", None) is None and _field(candidate, "trending_stars_today", None) is None):
            momentum_notes.add("部分项目暂无可用增量数据。")
    notes.extend(sorted(momentum_notes))
    return notes


def _copy(review: Any, field: str, fallback: str) -> str:
    text = _safe_text(_field(review, field, fallback), 240 if field == "highlight_zh" else 160)
    return text or fallback


def _date_text(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _safe_url(value: Any) -> str | None:
    url = str(value or "")
    return url if _GITHUB_REPOSITORY_URL.fullmatch(url) else None


def _escape_link_url(url: str) -> str:
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _allowlisted(value: Any, allowed: frozenset[str] | dict[str, str]) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else "unknown"


def _safe_run_id(value: Any) -> str:
    run_id = str(value).strip()
    if re.fullmatch(r"run-\d+", run_id):
        return run_id
    try:
        return str(UUID(run_id))
    except (TypeError, ValueError, AttributeError):
        return "unknown"


def _safe_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _escape_markdown(text: str) -> str:
    return text.translate(_MARKDOWN_ESCAPES)
