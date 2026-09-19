#!/usr/bin/env bash
# 装一个 launchd 定时任务，每天早上自动跑完整条流水线。
#
# 为什么是 launchd 而不是 cron / 云端定时：
# 本地 ASR（Parakeet MLX）是覆盖率的兜底层，它必须跑在这台 Apple Silicon 机器上。
# 把调度放在云上就得把音频传出去，成本和覆盖率都不划算。
#
# launchd 相对 cron 的实际差别：机器在预定时间睡着时，cron 那一次直接丢掉，
# launchd 会在唤醒后补跑（StartCalendarInterval 的行为）。笔记本场景这条很关键。
#
# 用法：
#   ./scripts/install_schedule.sh            # 装，默认每天 07:00
#   ./scripts/install_schedule.sh 6 30       # 每天 06:30
#   ./scripts/install_schedule.sh --uninstall

set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="$(pwd)"
LABEL="com.podcast.newsletter.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载 $LABEL"
  exit 0
fi

HOUR="${1:-7}"
MINUTE="${2:-0}"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PROJECT/run_daily.sh</string>
  </array>

  <key>WorkingDirectory</key><string>$PROJECT</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>

  <!-- 转录和模型调用都要网络和 Homebrew 的二进制，PATH 必须显式给。
       launchd 的默认 PATH 不含 /opt/homebrew/bin，ffmpeg 和 yt-dlp 会找不到。 -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
    <key>HF_HUB_DISABLE_XET</key><string>1</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>

  <key>StandardOutPath</key><string>$PROJECT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/logs/launchd.err.log</string>

  <!-- 失败了不要立刻重跑：重跑意味着重新转录、重新调模型，
       而且已成功的渠道会被发第二遍。等明天那一次。 -->
  <key>KeepAlive</key><false/>
  <key>RunAtLoad</key><false/>

  <!-- 转录是算力密集的，但这是后台任务，不该和前台抢资源 -->
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>5</integer>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf '已安装 %s\n' "$LABEL"
printf '  每天 %02d:%02d 自动跑 run_daily.sh\n' "$HOUR" "$MINUTE"
printf '  日志：%s/logs/\n' "$PROJECT"
echo ""
echo "立刻试跑一次：  launchctl kickstart -p gui/$(id -u)/$LABEL"
echo "查看状态：      launchctl print gui/$(id -u)/$LABEL | head -20"
echo "卸载：          ./scripts/install_schedule.sh --uninstall"
echo ""
echo "注意：机器在预定时间处于睡眠时，launchd 会在唤醒后补跑。"
echo "      要让它按时唤醒机器：pmset repeat wakeorpoweron MTWRFSU $(printf '%02d:%02d' "$HOUR" "$((MINUTE > 5 ? MINUTE - 5 : 0))"):00"
