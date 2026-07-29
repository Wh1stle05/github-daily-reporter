# GitHub Daily Reporter

这是一个两阶段的 GitHub 中文日报：Python 负责采集、快照和确定性评分，Hermes 只负责一次受限的语义筛选与写作，Python 再校验并直接调用 Telegram Bot API 投递。

榜单固定为两条消息：

- 成长项目榜：`1-9,999 Stars`
- 万星增量榜：`>=10,000 Stars`

每榜最多发送 10 个项目。Python 分数是事实字段，Agent 不能修改。

## 运行流程

```text
Hermes Cron --no-agent
  -> deploy/scripts/github-daily-runner.sh
  -> cli hybrid
  -> Trending + GitHub Search + Hacker News + GitHub metadata
  -> SQLite snapshot + cohort scoring
  -> data/runs/github-daily-report-YYYY-MM-DD/
  -> one `hermes -z` editorial session (900s process-group timeout)
  -> attempt report validation and atomic promotion
  -> Telegram: growth first, mature second
```

`--no-agent` 只作用于 Cron 外层。固定 Python wrapper 会启动一个独立的 `hermes -z`，不会恢复旧会话，也不会进入 quality-review/rank 修复链。

## 安装

```bash
cd "$HOME/workspace/github-daily-reporter"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

在 `.env` 中设置 `GITHUB_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。LLM 凭据由 Hermes profile 管理；仓库不保存任何密钥。

先做静态检查和一次手动运行：

```bash
.venv/bin/github-daily-reporter doctor --config config/reporter.yaml
.venv/bin/github-daily-reporter hybrid --config config/reporter.yaml
```

部署 wrapper 并创建 Cron：

```bash
install -m 700 deploy/scripts/github-daily-runner.sh "$HOME/.hermes/scripts/github-daily-runner.sh"
hermes cron create '0 9 * * *' '' \
  --name github-daily-reporter \
  --script github-daily-runner.sh \
  --no-agent \
  --workdir "$HOME/workspace/github-daily-reporter"
```

不要添加 Hermes `--deliver`，否则会和 Python 的 Telegram 投递重复。创建后检查 `hermes cron status`，确认时区和下一次运行时间。

## 运行产物

每次运行使用日期目录：

```text
data/runs/github-daily-report-YYYY-MM-DD/
├── collection.json
├── editorial-input.json
├── evidence/
├── attempts/<attempt_id>/
├── growth-report.md
├── mature-report.md
└── run-status.json
```

索引包含每个 cohort 20 个 primary 和最多 5 个 reserve。README 只放有界摘要，较大证据放在 `evidence/`。Agent 失败、超时、缺文件、越界 URL、重复项目或篡改分数都会使运行失败，旧报告不会被重新投递。

## 数据和评分

来源失败会记录为 `partial`；全部失败时不启动 Agent。Velocity 优先使用精确值，其次使用有时间戳的本地快照估算，再其次使用 Trending proxy；缺失信号保持 unavailable，不臆造数字。

成长榜权重为 `35/20/20/15/5/5`，万星榜为 `50/20/10/10/5/5`，分别对应绝对增长、相对增长、来源证据、活跃度、HN、Popularity。最终分数在加权后四舍五入到一位小数，报告显示：

```text
- 综合评分：82.4/100
```

## Telegram 和故障恢复

两条消息严格串行发送，纯文本模式，单条限制为 4,096 个 UTF-16 code units。网络超时、传输错误、429 和 5xx 最多重试三次；投递状态、digest、Telegram `message_id` 和 pending parts 写入 SQLite。重复运行同一 digest 不会重复发送已确认的 part，但 Telegram 接受后本地崩溃仍有 at-least-once 的极小重复窗口。

错误通知使用独立状态，不会覆盖报告 pending 状态，也不会递归触发新的错误通知。下一次运行先处理可恢复的 pending parts。

## 旧入口

生产入口只有 `cli hybrid` 和 `deploy/scripts/github-daily-runner.sh`。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

启用定时任务前，必须用真实 GitHub、Hermes 和 Telegram 凭据完成一次手动 `hybrid` 运行，并记录运行目录、source health、Agent 使用记录、报告文件和两条 Telegram `message_id` 的顺序。DDGS 当前只提供搜索，不保证网页提取；Web Fetch 是可选核验，失败不阻塞主流程。
