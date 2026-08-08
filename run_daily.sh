#!/usr/bin/env bash
# 每日播客 newsletter 流水线。
#
# 分两段：本脚本跑完所有确定性工作（抓取/转录/压缩），
# 停在 data/digest_pack.json；然后由 Claude 读 prompts/daily_digest_prompt.md
# 产出 data/judgments.json；最后 render_newsletter.py 排版+推送。
#
# 用法：
#   ./run_daily.sh              # 抓取 + 转录 + 压缩，产出 digest_pack.json
#   ./run_daily.sh --days 3     # 改回看窗口
#   ./run_daily.sh --no-asr     # 跳过本地转录（只看免费快路径）
#   ./run_daily.sh --dry        # 干跑，不写 seen 状态（可重复测试）

set -uo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export HF_HUB_DISABLE_XET=1

DAYS=1.5
ASR_FLAG=""
DRY_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --no-asr) ASR_FLAG="--no-asr"; shift ;;
    --dry) DRY_FLAG="--ignore-seen --no-mark"; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

LOG_DIR="logs"; mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/daily-$STAMP.log"

step() { echo ""; echo "===== $1 ====="; }
run()  { echo "\$ $*"; "$@" 2>&1 | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }

{
echo "播客 newsletter 流水线  $(date '+%F %T')  窗口=${DAYS}天"

step "1/5 抓订阅列表新集"
run python3 pipeline/fetch_episodes.py --days "$DAYS" $DRY_FLAG \
    --feeds data/feeds.json --out data/episodes.json || exit 1

step "2/5 抓盲区 watchlist 新集"
run python3 pipeline/fetch_episodes.py --days "$DAYS" $DRY_FLAG --max-per-show 2 \
    --feeds data/watchlist_feeds.json --out data/watchlist_episodes.json || exit 1

step "3/5 取正文（RSS全文 -> 官方transcript -> YouTube字幕 -> 官网 -> 本地转录）"
run python3 pipeline/get_transcript.py --episodes data/episodes.json \
    --out data/transcripts_index.json --workers 2 $ASR_FLAG
run python3 pipeline/get_transcript.py --episodes data/watchlist_episodes.json \
    --out data/transcripts_index_watchlist.json --workers 2 $ASR_FLAG

step "4/5 压成证据包"
run python3 pipeline/build_digest.py || exit 1

step "5/5 覆盖率体检"
run python3 pipeline/coverage_report.py

echo ""
echo "==================================================================="
echo "确定性部分完成。接下来："
echo "  1) 让 Claude 读 prompts/daily_digest_prompt.md 和 data/digest_pack.json，"
echo "     产出 data/judgments.json"
echo "  2) python3 pipeline/render_newsletter.py --webhook       # 发群"
echo "     python3 pipeline/render_newsletter.py --webhook --send # 群 + 私聊带附件"
echo "  3) python3 pipeline/sync_bitable.py                      # 同步多维表格"
echo "日志：$LOG"
echo "==================================================================="
} 2>&1 | tee -a "$LOG"
