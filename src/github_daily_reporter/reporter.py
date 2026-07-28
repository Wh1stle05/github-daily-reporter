"""Self-contained daily report pipeline: collect → LLM → save → Telegram."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys

import httpx

from github_daily_reporter.cli import run_collection
from github_daily_reporter.config import load_config
from github_daily_reporter.models import CollectionEnvelope, RepositoryCandidate

REPORT_PROMPT = """你是 GitHub 开源项目日报编辑。以下 JSON 是今日采集的开源项目数据，请据此生成中文日报。

要求：
1. 综合项目质量、社区热度、新鲜度排序，最多选 10 个
2. 排除空壳、纯营销、无实际代码、描述无法验证的项目
3. 只使用 JSON 中的事实，不要猜测
4. 如果候选项目质量普遍较低，宁缺毋滥

输出简洁的中文 Markdown，控制在 3500 字符以内：

# GitHub 每日趋势 · YYYY-MM-DD

## 今日精选

### 1. [owner/repo](URL)
一句话说明项目解决的问题。
- 信号：⭐ 总数；主要语言；来源
- 看点：为什么值得关注

## 快速观察
2-3 条趋势总结。

## 数据说明
仅在来源失败时出现。
"""


def save_collection_json(envelope: CollectionEnvelope, run_dir: Path) -> Path:
    """Save raw collection data to disk."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "collection.json"
    data = envelope.model_dump(mode="json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_report_md(report: str, run_dir: Path) -> Path:
    """Save the generated report to disk."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.md"
    path.write_text(report, encoding="utf-8")
    return path


async def generate_report(
    candidates: list[RepositoryCandidate],
    api_key: str,
    model: str = "deepseek-v4-flash",
    base_url: str = "https://api.deepseek.com",
) -> str:
    """Call the LLM API to produce a Chinese daily report."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    repo_summaries = []
    for c in candidates[:50]:
        sources = ", ".join(sorted(c.discovery_sources))
        repo_summaries.append(
            f"- {c.full_name} | ⭐{c.stars_total} | lang={c.primary_language or 'N/A'} "
            f"| src=[{sources}] | desc={c.description or 'N/A'}"
        )

    user_content = f"今日日期：{today}\n\n候选项目列表（共 {len(candidates)} 个）：\n" + "\n".join(repo_summaries)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": REPORT_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.3,
                "max_tokens": 4000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def send_telegram(message: str, bot_token: str, chat_id: str) -> None:
    """Send a message via Telegram Bot API."""
    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram API returned HTTP {resp.status}")


def report(config_path_str: str) -> None:
    """Full pipeline: collect → save JSON → LLM report → save MD → Telegram."""
    config_path = Path(config_path_str).expanduser().resolve()
    config = load_config(config_path)

    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    project_root = config_path.parent.parent
    run_dir = project_root / "data" / "reports" / today_str

    # 1. Collect
    print(f"[reporter] Collecting data...", file=sys.stderr)
    envelope = asyncio.run(run_collection(config_path))
    json_path = save_collection_json(envelope, run_dir)
    print(f"[reporter] Collection saved → {json_path}", file=sys.stderr)

    if envelope.status == "failed":
        print("[reporter] All sources failed; cannot generate report", file=sys.stderr)
        sys.exit(1)

    # 2. Generate report via LLM
    api_key = os.environ.get("LLM_API_KEY") or config.github_token.get_secret_value()
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")

    print(f"[reporter] Generating report via {model}...", file=sys.stderr)
    report_text = asyncio.run(generate_report(envelope.candidates, api_key, model, base_url))
    md_path = save_report_md(report_text, run_dir)
    print(f"[reporter] Report saved → {md_path}", file=sys.stderr)

    # 3. Send via Telegram
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        try:
            send_telegram(report_text, bot_token, chat_id)
            print(f"[reporter] Telegram delivered ✅", file=sys.stderr)
        except Exception as e:
            print(f"[reporter] Telegram delivery failed: {e}", file=sys.stderr)
    else:
        print(f"[reporter] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping Telegram", file=sys.stderr)

    # 4. Print report to stdout (for cron capture)
    print(report_text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="github-daily-reporter")
    parser.add_argument("--config", type=str, default="config/reporter.yaml")
    args = parser.parse_args()
    report(args.config)


if __name__ == "__main__":
    main()
