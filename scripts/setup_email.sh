#!/usr/bin/env bash
# 交互式配置邮件发送，并发一封测试邮件。
#
# 为什么要有这个脚本：SMTP 密码不该经过聊天、不该出现在命令历史里、
# 也不该被别人代填。这里用 read -s 直接从你的键盘读到 .env，
# 中间不落任何临时文件，.env 权限设成 600。
#
# Gmail 注意：账号密码从 2022 年 5 月起就不能用于 SMTP 了，必须是
# 16 位的「应用专用密码」（且账号要先开两步验证）：
#   两步验证  https://myaccount.google.com/signinoptions/twosv
#   应用密码  https://myaccount.google.com/apppasswords
#
# 用法：./scripts/setup_email.sh

set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=".env"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"

ask() {  # ask VAR "提示" "默认值"
  local var="$1" prompt="$2" default="${3:-}" cur val
  cur="$(grep -E "^${var}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  [[ -n "$cur" ]] && default="$cur"
  read -r -p "$prompt${default:+ [$default]}: " val
  echo "${val:-$default}"
}

echo "配置每日 newsletter 的邮件发送"
echo "───────────────────────────────────────────"
HOST=$(ask SMTP_HOST "SMTP 服务器" "smtp.gmail.com")
PORT=$(ask SMTP_PORT "端口（587=STARTTLS / 465=SSL）" "587")
USER=$(ask SMTP_USER "发信邮箱")
FROM=$(ask EMAIL_FROM "From 显示名" "Podcast Screen <$USER>")
TO=$(ask EMAIL_TO "收件人（多个用逗号分隔）")

echo ""
if [[ "$HOST" == *gmail* ]]; then
  echo "⚠️  Gmail 必须用应用专用密码（16 位，形如 abcd efgh ijkl mnop）。"
  echo "    用登录密码一定失败：535 Username and Password not accepted。"
fi
# -s 不回显；密码不进 shell 历史、不落临时文件
read -r -s -p "密码（输入时不显示）: " PASS
echo ""
PASS="${PASS// /}"        # Gmail 应用密码带空格，去掉

# 原子重写：先剔除旧的邮件相关行，再追加
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
grep -vE "^(SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_PASS|EMAIL_FROM|EMAIL_TO)=" \
  "$ENV_FILE" > "$TMP" 2>/dev/null || true
{
  echo ""
  echo "# 邮件发送（由 scripts/setup_email.sh 写入）"
  echo "SMTP_HOST=$HOST"
  echo "SMTP_PORT=$PORT"
  echo "SMTP_USER=$USER"
  echo "SMTP_PASS=$PASS"
  echo "EMAIL_FROM=$FROM"
  echo "EMAIL_TO=$TO"
} >> "$TMP"
mv "$TMP" "$ENV_FILE"; chmod 600 "$ENV_FILE"
trap - EXIT
echo "✓ 已写入 .env（权限 600，已在 .gitignore 里）"

echo ""
echo "发送测试邮件…"
if python3 pipeline/deliver_email.py \
     --judgments "${1:-data/alan_judgments.json}" \
     --pack "${2:-data/alan_pack.json}"; then
  echo ""
  echo "✓ 发送成功。以后每天自动发："
  echo "    ./run_daily.sh"
else
  echo ""
  echo "发送失败。最常见的两个原因："
  echo "  535 Username and Password not accepted"
  echo "      -> 用的是登录密码而不是应用专用密码。去 https://myaccount.google.com/apppasswords 生成"
  echo "  Connection refused / timed out"
  echo "      -> 端口不对。587 配 STARTTLS，465 配 SSL，二者不能混"
  exit 1
fi
