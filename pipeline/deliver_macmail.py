#!/usr/bin/env python3
"""通过 macOS Mail.app 发信。

为什么要有这条路：Mail.app 已经登录了用户的账号（iCloud / Exchange / Gmail），
所以发信**不需要任何密码**——不用应用专用密码、不用把凭据写进 .env、
不用担心 Gmail 从 2022 年起拒绝账号密码做 SMTP 认证那套。
对于「这台 Mac 上每天跑一次」的场景，这是摩擦最小的方案。

代价与边界（不粉饰）：
  - 只能在装了 Mail.app 并已配好账号的 macOS 上跑，服务器上没有这条路
  - 需要一次「自动化」权限授权（首次调用会弹窗）
  - Mail.app 必须能运行；锁屏状态下通常没问题，但用户手动退出 Mail 就发不了
  - 发件人是 Mail 里的账号，不能任意伪造 From

正文走文件而不是 osascript 参数：一封 newsletter 的 HTML 有三四万字符，
塞进命令行参数会撞 ARG_MAX，而且引号转义极易出错。AppleScript 直接读文件更稳。
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, ROOT, load_json


def _osa(script, timeout=120):
    p = subprocess.run(["osascript", "-"], input=script, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def available():
    """Mail.app 能用吗。返回 (bool, 说明)。"""
    if sys.platform != "darwin":
        return False, "非 macOS"
    rc, out, err = _osa('tell application "Mail" to get email addresses of every account')
    if rc != 0:
        if "Not authorized" in err or "-1743" in err:
            return False, ("没有自动化权限。系统设置 -> 隐私与安全性 -> 自动化，"
                           "允许终端控制「邮件」")
        return False, f"Mail.app 不可用: {err[:140]}"
    addrs = [a.strip() for a in out.split(",") if a.strip()]
    if not addrs:
        return False, "Mail.app 里没有配置任何账号"
    return True, ", ".join(addrs)


def _escape(s):
    """AppleScript 字符串转义。只有反斜杠和双引号需要处理。"""
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"')


def send(subject, html, to, sender=None, attachments=None, dry=False):
    """发一封 HTML 邮件。返回 (ok, 说明)。

    dry=True 只建草稿不发送，用来验证链路。
    """
    ok, info = available()
    if not ok:
        return False, info
    if isinstance(to, str):
        to = [t.strip() for t in to.replace(";", ",").split(",") if t.strip()]
    if not to:
        return False, "没有收件人"

    # 正文落临时文件，AppleScript 去读——避免 ARG_MAX 和转义地狱
    fd, path = tempfile.mkstemp(suffix=".html", prefix="podcast-mail-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)

        recips = "\n".join(
            f'      make new to recipient at end of to recipients '
            f'with properties {{address:"{_escape(a)}"}}' for a in to)
        atts = "\n".join(
            f'      make new attachment with properties '
            f'{{file name:(POSIX file "{_escape(os.path.abspath(p))}")}} '
            f'at after the last paragraph'
            for p in (attachments or []) if os.path.exists(p))
        sender_line = (f'  set sender of m to "{_escape(sender)}"\n'
                       if sender else "")
        action = "  save m" if dry else "  send m"

        script = f'''
set htmlText to (read POSIX file "{_escape(path)}" as «class utf8»)
tell application "Mail"
  set m to make new outgoing message with properties ¬
      {{subject:"{_escape(subject)}", visible:false}}
  set html content of m to htmlText
{sender_line}  tell m
{recips}
{atts}
  end tell
{action}
  return "ok"
end tell
'''
        rc, out, err = _osa(script, timeout=180)
        if rc != 0 or "ok" not in out:
            return False, f"Mail.app 报错: {(err or out)[:220]}"
        verb = "已存草稿" if dry else "已发送"
        return True, f"{verb} -> {', '.join(to)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    import argparse
    from datetime import datetime, timedelta, timezone
    ap = argparse.ArgumentParser(description="通过 macOS Mail.app 发 newsletter")
    ap.add_argument("--judgments", default=os.path.join(DATA, "judgments.json"))
    ap.add_argument("--pack", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--to", help="收件人，逗号分隔（默认取 .env 的 EMAIL_TO）")
    ap.add_argument("--from", dest="sender", help="发件账号（默认 Mail 的默认账号）")
    ap.add_argument("--draft", action="store_true", help="只存草稿不发送")
    ap.add_argument("--check", action="store_true", help="只检查 Mail.app 可用性")
    args = ap.parse_args()

    ok, info = available()
    print(f"Mail.app: {'可用 — 账号 ' + info if ok else '不可用 — ' + info}")
    if args.check:
        return 0 if ok else 1
    if not ok:
        return 1

    j = load_json(args.judgments, None)
    if not j:
        print(f"读不到 {args.judgments}", file=sys.stderr)
        return 2
    idx = {i["episode_id"]: i for i in (load_json(args.pack, {}) or {}).get("items", [])}
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    import deliver_email as EM
    to = args.to or os.environ.get("EMAIL_TO", "")
    if not to:
        print("没有收件人：用 --to 指定，或在 .env 里配 EMAIL_TO", file=sys.stderr)
        return 2

    subj = EM.subject(j, day)
    html = EM.render_html(j, idx, day)
    print(f"主题：{subj}")
    print(f"收件人：{to}")
    ok, info = send(subj, html, to, sender=args.sender, dry=args.draft)
    print(("✓ " if ok else "✗ ") + info)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
