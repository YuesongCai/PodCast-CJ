#!/usr/bin/env bash
# 每日播客 newsletter —— 端到端，无人值守。
#
#   抓新集 -> 取正文 -> 压成证据包 -> Claude 判断 -> 渲染 -> 分发
#
# 判断环节（judge.py）过去需要人把 prompt 喂给 Claude，现在是自动的，
# 所以这个脚本可以直接挂 cron / launchd。见 README「定时跑」。
#
# 用法：
#   ./run_daily.sh                # 完整跑通并投递
#   ./run_daily.sh --days 3       # 改回看窗口（默认 1.5 天）
#   ./run_daily.sh --no-asr       # 跳过本地转录（只走免费快路径，快但覆盖率低）
#   ./run_daily.sh --no-send      # 跑到渲染为止，不投递
#   ./run_daily.sh --dry          # 干跑：不写 seen 状态、不实际发送（可重复测试）
#   ./run_daily.sh --stop-at-pack # 只跑确定性部分，停在 digest_pack.json
#
# 退出码：0 全部成功；1 有步骤失败；2 参数错误。

set -uo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export HF_HUB_DISABLE_XET=1
export PYTHONUNBUFFERED=1

DAYS=1.5
ASR_FLAG=""
DRY_FETCH=""
DRY_SEND=""
SEND=1
STOP_AT_PACK=0
JUDGE_BACKEND="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days)         DAYS="$2"; shift 2 ;;
    --no-asr)       ASR_FLAG="--no-asr"; shift ;;
    --no-send)      SEND=0; shift ;;
    --dry)          DRY_FETCH="--ignore-seen --no-mark"; DRY_SEND="--dry-run"; shift ;;
    --stop-at-pack) STOP_AT_PACK=1; shift ;;
    --backend)      JUDGE_BACKEND="$2"; shift 2 ;;
    -h|--help)      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

LOG_DIR="logs"; mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/daily-$STAMP.log"
FAILED=()

step() { echo ""; echo "===== $* ====="; }

# 跑一步。第一个参数是步骤名，用于失败汇总；其余是命令。
run() {
  local name="$1"; shift
  echo "\$ $*"
  if "$@"; then
    return 0
  fi
  local rc=$?
  echo "!! [$name] 失败（退出码 $rc）"
  FAILED+=("$name")
  return $rc
}

{
echo "播客 newsletter  $(date '+%F %T')  窗口=${DAYS}天  后端=${JUDGE_BACKEND}"
[[ -n "$DRY_FETCH" ]] && echo "（干跑模式：不写 seen 状态，不实际发送）"

step "1/7 抓订阅列表新集"
run fetch-subscribed python3 pipeline/fetch_episodes.py --days "$DAYS" $DRY_FETCH \
    --feeds data/feeds.json --out data/episodes.json \
  || { echo "抓不到订阅列表，后面没法继续"; exit 1; }

step "2/7 抓盲区 watchlist 新集"
# 盲区是补充，抓不到不阻断——订阅列表才是主干
run fetch-watchlist python3 pipeline/fetch_episodes.py --days "$DAYS" $DRY_FETCH \
    --max-per-show 2 --feeds data/watchlist_feeds.json --out data/watchlist_episodes.json

step "3/7 取正文（RSS全文 -> 官方transcript -> YouTube字幕 -> 官网 -> 本地转录）"
run transcript-subscribed python3 pipeline/get_transcript.py \
    --episodes data/episodes.json --out data/transcripts_index.json \
    --workers 2 $ASR_FLAG
run transcript-watchlist python3 pipeline/get_transcript.py \
    --episodes data/watchlist_episodes.json \
    --out data/transcripts_index_watchlist.json --workers 2 $ASR_FLAG

step "4/7 压成证据包"
run build-digest python3 pipeline/build_digest.py \
  || { echo "没有证据包就没有判断，中止"; exit 1; }

step "5/7 覆盖率体检"
run coverage python3 pipeline/coverage_report.py

if [[ "$STOP_AT_PACK" == "1" ]]; then
  echo ""
  echo "已停在 data/digest_pack.json（--stop-at-pack）。日志：$LOG"
  exit 0
fi

step "6/7 判断（Claude 筛选：值不值得听）"
# 这是唯一的非确定性环节。judge.py 自带校验+修复循环，
# 校验不过不会落盘，所以这里失败就必须中止——宁可今天不发，
# 也不要发一份编造 episode_id 或全判「值得听」的 newsletter。
run judge python3 pipeline/judge.py --backend "$JUDGE_BACKEND" \
  || { echo ""; echo "判断没通过校验，不投递。上一份判断仍在 data/judgments.json"; exit 1; }

step "7/7 渲染 + 投递"
if [[ "$SEND" == "1" ]]; then
  # 渠道之间失败隔离：一个渠道挂了不影响其余，退出码记进汇总
  run deliver python3 pipeline/deliver.py $DRY_SEND
else
  run render python3 pipeline/render_newsletter.py
  echo "（--no-send：已生成但未投递）"
fi

# 存档不影响投递成败，放最后
if [[ -n "${LARK_BASE_TOKEN:-}" ]] || [[ -f data/bitable.json ]]; then
  step "存档到多维表格"
  run bitable python3 pipeline/sync_bitable.py
fi

echo ""
echo "==================================================================="
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "✓ 全部完成  $(date '+%F %T')"
else
  echo "⚠ 完成，但这些步骤失败了：${FAILED[*]}"
fi
echo "日志：$LOG"
echo "==================================================================="
[[ ${#FAILED[@]} -eq 0 ]] || exit 1
} 2>&1 | tee -a "$LOG"

exit "${PIPESTATUS[0]}"
