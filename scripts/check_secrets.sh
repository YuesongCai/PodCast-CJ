#!/usr/bin/env bash
# 提交前的凭据检查。仓库是公开的，跑这个比事后 rotate 便宜得多。
#
# 查两类：
#   1) .env 里的真实值有没有出现在将要提交的文件里
#      （踩过的真实案例：渲染出的 newsletter 内嵌多维表格链接，而那个 URL 带 Base token）
#   2) 常见凭据的形状（webhook / bot token / open_id / API key）
#
# 用法：./scripts/check_secrets.sh        退出码 0 = 干净
set -uo pipefail
cd "$(dirname "$0")/.."
exec python3 - <<'PY'
import os, re, subprocess, sys

# 值本身不是秘密的键：路径、域名前缀、表名之类，放进来免得每次都误报
NOT_SECRET = {"LARK_CLI", "LARK_BASE_TABLE", "PODCAST_JUDGE_MODEL",
              "SMTP_HOST", "SMTP_PORT", "EMAIL_FROM", "EMAIL_TO"}

secrets = {}
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k not in NOT_SECRET and len(v) >= 12:
            secrets[k] = v

PATTERNS = {
    "飞书群 webhook": r"open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-f-]{20,}",
    "Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "飞书 open_id": r"\bou_[0-9a-f]{32}\b",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "sk- 形式的 API key": r"\bsk-[A-Za-z0-9_-]{20,}",
    "飞书 Base token": r"\b[A-Za-z0-9]{20,30}(?=/?(base|wiki)/)",
}

# git 眼里将要进仓库的文件：已跟踪的 + 未被 ignore 的未跟踪文件
files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"],
                       capture_output=True, text=True).stdout.split("\n")
files = [f for f in files if f and os.path.isfile(f)]

hits = []
for f in files:
    try:
        txt = open(f, encoding="utf-8", errors="ignore").read()
    except OSError:
        continue
    for k, v in secrets.items():
        if v in txt:
            hits.append(f"{f}: 含 .env 里 {k} 的真实值")
    for name, pat in PATTERNS.items():
        for m in re.findall(pat, txt):
            # .env.example 里的占位符不算
            if "xxxx" in str(m).lower() or "example" in f:
                continue
            hits.append(f"{f}: 疑似{name} — {str(m)[:36]}…")

print(f"扫描 {len(files)} 个文件，保护 {len(secrets)} 个 .env 值")
if hits:
    print("\n✗ 发现问题：")
    for h in dict.fromkeys(hits):
        print(f"  {h}")
    print("\n处理：加进 .gitignore，或从文件里去掉。已经提交过的要 rotate 凭据。")
    sys.exit(1)
print("✓ 未发现凭据泄漏")
PY
