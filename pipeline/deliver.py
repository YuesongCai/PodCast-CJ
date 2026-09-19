#!/usr/bin/env python3
"""统一投递入口：渲染一次，分发到所有已配置的渠道。

为什么要有这一层，而不是各渠道各自为政：

**渠道之间必须失败隔离。** 推送是转发层，没有存储价值——判断已经落在
`data/judgments.json` 和多维表格里了。所以 Telegram 挂了不应该让邮件也发不出去，
更不应该让整条流水线的退出码变成失败、进而让定时任务重跑一遍（重跑意味着
重新转录、重新调模型，既费算力又会把已成功的渠道发第二遍）。

这里的约定：
  - 每个渠道独立 try，一个失败不影响其余；
  - 只对**已配置**的渠道计成败，没配的渠道跳过不算失败；
  - 退出码：全部成功 0；部分失败 1；一个渠道都没配 2。

用法：
    python3 pipeline/deliver.py                  # 发到所有已配置的渠道
    python3 pipeline/deliver.py --only email     # 只发邮件
    python3 pipeline/deliver.py --dry-run        # 渲染 + 检查配置，不实发
"""
import argparse
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, ROOT, load_json

# macmail 排在 email 前面：它不需要任何密码（Mail.app 已经登录了账号），
# 所以在这台 Mac 上它是首选，SMTP 是没有 Mail.app 时的退路。
# 两者都配了的话只发一次——避免收件人收到两封一样的。
CHANNELS = ("macmail", "email", "telegram", "lark_webhook", "lark_dm")


def configured(name):
    """这个渠道配齐了吗。没配的渠道不算失败，只是跳过。"""
    E = os.environ.get
    if name == "macmail":
        if not (E("EMAIL_TO") or E("SMTP_USER")):
            return False
        try:
            import deliver_macmail as MM
            return MM.available()[0]
        except Exception:
            return False
    if name == "email":
        return all(E(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")) and \
            bool(E("EMAIL_TO") or E("SMTP_USER"))
    if name == "telegram":
        return bool(E("TELEGRAM_BOT_TOKEN") and E("TELEGRAM_CHAT_ID"))
    if name == "lark_webhook":
        return bool(E("LARK_WEBHOOK"))
    if name == "lark_dm":
        return bool(E("LARK_USER_ID")) and os.path.exists(
            E("LARK_CLI", "/opt/homebrew/bin/lark-cli"))
    return False


def main():
    ap = argparse.ArgumentParser(description="把判断分发到所有已配置的渠道")
    ap.add_argument("--judgments", default=os.path.join(DATA, "judgments.json"))
    ap.add_argument("--pack", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--only", nargs="+", choices=CHANNELS,
                    help="只发这些渠道（默认发所有已配置的）")
    ap.add_argument("--dry-run", action="store_true",
                    help="渲染并检查配置，不实际发送")
    args = ap.parse_args()

    j = load_json(args.judgments, None)
    if not j or not j.get("items"):
        print(f"读不到判断或判断为空：{args.judgments}\n"
              f"先跑 python3 pipeline/judge.py", file=sys.stderr)
        return 2
    pack = load_json(args.pack, {}) or {}
    idx = {i["episode_id"]: i for i in pack.get("items", [])}
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    # markdown 全文：邮件之外的渠道拿它当附件，也是留档
    import render_newsletter as RN
    md = RN.render(j, idx)
    md_path = os.path.join(ROOT, "newsletters", f"podcast-daily-{day}.md")
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    import deliver_email as EM
    html_path = os.path.join(ROOT, "newsletters", f"podcast-daily-{day}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(EM.render_html(j, idx, day))

    items = j["items"]
    must = sum(1 for x in items if x.get("verdict") == "must_listen")
    print(f"播客早报 {day}：{len(items)} 集，{must} 集建议听")
    print(f"  md   {md_path}")
    print(f"  html {html_path}")

    targets = list(args.only) if args.only else [c for c in CHANNELS if configured(c)]
    # 同一封邮件不发两遍：macmail 能用就不再走 SMTP
    if not args.only and "macmail" in targets and "email" in targets:
        targets.remove("email")
    if not targets:
        print("\n没有任何渠道配置好。至少配一个（见 .env.example）：\n"
              "  邮件      EMAIL_TO（配好 Mail.app 即可，不需要密码）\n"
              "            或 SMTP_HOST / SMTP_USER / SMTP_PASS / EMAIL_TO\n"
              "  telegram TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID\n"
              "  飞书      LARK_WEBHOOK 或 LARK_USER_ID", file=sys.stderr)
        return 2

    print(f"\n投递到：{', '.join(targets)}")
    results = {}
    for ch in targets:
        if not configured(ch):
            results[ch] = (False, "未配置")
            print(f"  {ch:<14} ✗ 未配置")
            continue
        try:
            if ch == "macmail":
                import deliver_macmail as MM
                to = os.environ.get("EMAIL_TO") or os.environ.get("SMTP_USER", "")
                if args.dry_run:
                    ok, info = True, f"[dry] {EM.subject(j, day)[:50]}… -> {to}"
                else:
                    ok, info = MM.send(EM.subject(j, day),
                                       EM.render_html(j, idx, day), to,
                                       sender=os.environ.get("MACMAIL_FROM"))
            elif ch == "email":
                ok, info = EM.send(j, idx, day, dry=args.dry_run)
            elif ch == "telegram":
                import deliver_telegram as TG
                if args.dry_run:
                    n = len(TG.paginate(TG._blocks(j, idx, day)))
                    ok, info = True, f"[dry] {n} 条消息 + md 附件"
                else:
                    ok, info = TG.send(j, idx, day, md_path)
            elif ch == "lark_webhook":
                ok, info = (True, "[dry] 交互卡片") if args.dry_run else \
                    RN.send_webhook(j, idx, day, md_path)
                info = info or "已发送"
            else:  # lark_dm
                summary = (f"📻 **播客早报 {day}** — {len(items)} 集，"
                           f"{must} 集建议听\n\n{j.get('today_in_one_line', '')}")
                ok, info = (True, "[dry] 摘要 + md 附件") if args.dry_run else \
                    RN.send_lark(md_path, summary)
                info = info or "已发送"
        except Exception as e:
            ok, info = False, f"{type(e).__name__}: {e}"
            if os.environ.get("PODCAST_DEBUG"):
                traceback.print_exc()
        results[ch] = (ok, info)
        print(f"  {ch:<14} {'✓' if ok else '✗'} {info}")

    failed = [c for c, (ok, _) in results.items() if not ok]
    ok_n = len(results) - len(failed)
    print(f"\n{ok_n}/{len(results)} 个渠道成功"
          + (f"，失败：{', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
