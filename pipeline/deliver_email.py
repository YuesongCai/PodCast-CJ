#!/usr/bin/env python3
"""把判断渲染成 HTML 邮件并通过 SMTP 发出。

这是最终形态的主渠道：给一份 podcast list，每天早上收一封邮件。
Telegram / 飞书是转发层，邮件才是那个「吃早饭时 scroll 一遍」的载体。

收件人是二级市场买方，整天读 sell-side research，所以产出是**英文**、
按研报的读法排版：结论先行、分栏头全大写、数据条紧凑、衬线正文。
措辞规格在 prompts/daily_digest_prompt.md，这里只负责排版和投递。

邮件 HTML 和网页 HTML 不是一回事，下面这些不是洁癖而是必须：

1. **全部 CSS 内联**。Gmail 会剥掉 `<style>` 块里的一部分规则，
   Outlook 桌面版用 Word 渲染引擎，`<style>` 支持更差。内联样式是唯一到处都认的。
2. **不用 flex / grid / position**。Outlook 的 Word 引擎完全不支持，
   布局会塌成一列乱码。用 table + 嵌套 div。
3. **max-width 640px**。超过这个宽度在 Outlook 预览窗格里会横向溢出。
4. **字体只用 Georgia / Arial 这类全平台都有的**。邮件里 @font-face 基本无效。
5. **必须带 text/plain 备份**（multipart/alternative）。
   纯文本客户端、以及一部分反垃圾评分器会看这个；只发 HTML 更容易进垃圾箱。
6. **主题行带数字**。收件箱列表里他只能看到这一行。

配置（.env）：
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587                # 587=STARTTLS, 465=SSL
    SMTP_USER=you@gmail.com
    SMTP_PASS=应用专用密码       # Gmail 要在账号安全里生成 App Password
    EMAIL_FROM=Podcast Screen <you@gmail.com>
    EMAIL_TO=cj@example.com,aaron@example.com
"""
import argparse
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, ROOT, load_json

# 版式按二级买方研报来：衬线正文 + 无衬线元数据 + 细横线分栏。
# 收件人整天读 sell-side note，版式对不上就没有「专业」那层信号。
INK = "#111318"
BODY = "#2b2f36"
MUTED = "#767d88"
LINE = "#dfe3e8"
RULE = "#111318"
ACCENT = {"must_listen": "#0b5c4a", "worth_skim": "#8a5a00", "skip": "#8b9099"}
CALL = {"must_listen": "LISTEN", "worth_skim": "SKIM", "skip": "SKIP"}
SECTION_TITLE = {"must_listen": "LISTEN", "worth_skim": "SKIM",
                 "skip": "SCREENED OUT"}
SOURCE_LABEL = {"youtube": "YouTube captions", "asr": "local ASR",
                "rss_inline": "RSS full text", "rss_transcript": "official transcript",
                "website": "show site", "notes_only": "show notes only"}

# 邮件客户端字体白名单窄得多：Georgia 和 Arial 在 Windows/Mac/iOS/Android 全都有。
SERIF = "Georgia,'Times New Roman',serif"
SANS = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,"
        "'Helvetica Neue',sans-serif")


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _minutes(v):
    """duration_min 来自各家 RSS，可能是 int、字符串、空值。统一成 int。"""
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return 0


def hhmm(minutes):
    """时长用 research note 的写法：28min / 1h15m。"""
    minutes = _minutes(minutes)
    if minutes <= 0:
        return "n/a"
    if minutes < 60:
        return f"{minutes}min"
    return (f"{minutes // 60}h{minutes % 60:02d}m" if minutes % 60
            else f"{minutes // 60}h")


def read_across(x):
    """字段改过名，旧名保留为别名（见 judge.py）。"""
    for k in ("read_across", "for_you"):
        v = (x.get(k) or "").strip()
        if v:
            return v
    return ""


def headline(j):
    for k in ("pm_summary", "today_in_one_line"):
        v = (j.get(k) or "").strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------- HTML

def _rule(weight=1, color=None, space="18px 0"):
    return (f'<div style="border-top:{weight}px solid {color or LINE};'
            f'font-size:0;line-height:0;margin:{space};">&nbsp;</div>')


def _section_head(label, right=""):
    """研报里的分栏头：全大写、加宽字距、下面一道细线。"""
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="margin:30px 0 14px;">'
            f'<tr><td style="border-bottom:1px solid {RULE};padding:0 0 6px;">'
            f'<span style="font:700 11px/1.4 {SANS};color:{INK};'
            f'letter-spacing:.14em;">{esc(label)}</span></td>'
            f'<td align="right" style="border-bottom:1px solid {RULE};padding:0 0 6px;">'
            f'<span style="font:11px/1.4 {SANS};color:{MUTED};'
            f'letter-spacing:.06em;">{esc(right)}</span></td>'
            f'</tr></table>')


def _card(x, meta, detailed):
    """一集。结论先行：标题 -> 元数据条 -> call 的理由 -> 支撑点 -> read-across。"""
    v = x.get("verdict", "worth_skim")
    color = ACCENT.get(v, MUTED)
    P = ['<div style="margin:0 0 22px;">']

    P.append(f'<div style="font:600 16px/1.42 {SERIF};color:{INK};margin:0 0 6px;">'
             f'{esc(x.get("title") or meta.get("title"))}</div>')

    # 元数据条：节目 · 时长 · CALL · 评分。CALL 用色块，扫读时先看到判断
    meta_bits = [f'<b style="color:{INK};font-weight:600;">'
                 f'{esc(meta.get("show") or x.get("show"))}</b>',
                 hhmm(meta.get("duration_min"))]
    call = (f'<span style="color:{color};font-weight:700;letter-spacing:.06em;">'
            f'{CALL.get(v, v.upper())}</span>')
    if x.get("score"):
        call += f'<span style="color:{MUTED};"> {x["score"]}/10</span>'
    meta_bits.append(call)
    P.append(f'<div style="font:12px/1.6 {SANS};color:{MUTED};margin:0 0 9px;">'
             f'{" &nbsp;·&nbsp; ".join(meta_bits)}</div>')

    if x.get("why"):
        P.append(f'<div style="font:15px/1.62 {SERIF};color:{BODY};margin:0 0 10px;">'
                 f'{esc(x["why"])}</div>')

    if detailed and x.get("key_points"):
        lis = "".join(f'<li style="margin:0 0 5px;">{esc(k)}</li>'
                      for k in x["key_points"][:6])
        P.append(f'<ul style="font:14px/1.6 {SERIF};color:{BODY};'
                 f'margin:0 0 10px;padding-left:18px;">{lis}</ul>')

    ra = read_across(x)
    if detailed and ra:
        P.append(f'<div style="font:13px/1.58 {SANS};color:{BODY};'
                 f'border-left:2px solid {color};padding:2px 0 2px 10px;'
                 f'margin:0 0 10px;"><b style="color:{INK};'
                 f'letter-spacing:.03em;">READ-ACROSS</b> &nbsp;{esc(ra)}</div>')

    bits = []
    if meta.get("link"):
        bits.append(f'<a href="{esc(meta["link"])}" style="color:{color};'
                    f'text-decoration:none;border-bottom:1px solid {LINE};">Episode</a>')
    if meta.get("youtube"):
        bits.append(f'<a href="{esc(meta["youtube"])}" style="color:{color};'
                    f'text-decoration:none;border-bottom:1px solid {LINE};">YouTube</a>')
    src = SOURCE_LABEL.get(meta.get("evidence_source"))
    if src:
        bits.append(f"Evidence: {src}")
    if meta.get("fidelity") == "notes_only":
        bits.append('<span style="color:#8a5a00;">no transcript — call made '
                    'on the description only</span>')
    if bits:
        P.append(f'<div style="font:11px/1.55 {SANS};color:{MUTED};">'
                 f'{" &nbsp;·&nbsp; ".join(bits)}</div>')
    P.append("</div>")
    return "".join(P)


def render_html(j, idx, day):
    items = j.get("items", [])
    dur = lambda x: _minutes(idx.get(x["episode_id"], {}).get("duration_min"))
    must = [x for x in items if x.get("verdict") == "must_listen"]
    skim = [x for x in items if x.get("verdict") == "worth_skim"]
    skipped = [x for x in items if x.get("verdict") == "skip"]
    total, saved = sum(dur(x) for x in items), sum(dur(x) for x in skipped)
    n_sub = sum(1 for x in items if x.get("section") == "subscribed")
    # 订阅的排前面：他自己订的优先级高于盲区补充
    keyf = lambda x: (x.get("section") != "subscribed", -(x.get("score") or 0))

    B = []

    # 报头：像研报的 masthead，日期右对齐
    B.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             f'border="0"><tr>'
             f'<td style="font:700 15px/1.3 {SANS};color:{INK};letter-spacing:.16em;">'
             f'PODCAST SCREEN</td>'
             f'<td align="right" style="font:12px/1.3 {SANS};color:{MUTED};">'
             f'{esc(day)}</td></tr></table>')
    B.append(_rule(2, RULE, "10px 0 0"))

    # 统计条：一行说清覆盖和筛掉多少
    B.append(f'<div style="font:12px/1.75 {SANS};color:{MUTED};margin:12px 0 0;">'
             f'<b style="color:{INK};">{len(items)}</b> episodes screened '
             f'({n_sub} subscribed / {len(items) - n_sub} blind-spot) &nbsp;·&nbsp; '
             f'<b style="color:{INK};">{hhmm(total)}</b> of audio &nbsp;·&nbsp; '
             f'<b style="color:{ACCENT["must_listen"]};">{len(must)} flagged to '
             f'listen</b> &nbsp;·&nbsp; {hhmm(saved)} screened out</div>')

    if headline(j):
        B.append(_section_head("PM SUMMARY"))
        B.append(f'<div style="font:15px/1.68 {SERIF};color:{INK};">'
                 f'{esc(headline(j))}</div>')

    for verdict, group, detailed in (("must_listen", must, True),
                                     ("worth_skim", skim, False)):
        if not group:
            continue
        B.append(_section_head(SECTION_TITLE[verdict],
                               f"{len(group)} of {len(items)}"))
        B.extend(_card(x, idx.get(x["episode_id"], {}), detailed)
                 for x in sorted(group, key=keyf))

    if skipped:
        B.append(_section_head("SCREENED OUT",
                               f"{len(skipped)} episodes · {hhmm(saved)} saved"))
        for x in sorted(skipped, key=keyf):
            m = idx.get(x["episode_id"], {})
            B.append(
                f'<div style="font:13px/1.58 {SANS};color:{MUTED};margin:0 0 10px;">'
                f'<b style="color:{BODY};font-weight:600;">{esc(m.get("show", ""))}</b> '
                f'&nbsp;·&nbsp; {esc(x.get("title"))} '
                f'<span style="color:{LINE};">|</span> {hhmm(m.get("duration_min"))}<br>'
                f'{esc(x.get("why", ""))}</div>')

    if j.get("gaps"):
        B.append(_section_head("NOT COVERED"))
        B.append(f'<ul style="font:12px/1.65 {SANS};color:{MUTED};'
                 f'margin:0;padding-left:18px;">'
                 + "".join(f"<li>{esc(g)}</li>" for g in j["gaps"]) + "</ul>")

    bt = (load_json(os.path.join(DATA, "bitable.json"), {}) or {}).get("url")
    foot = ["Transcripts sourced from YouTube captions, local ASR, and publisher RSS "
            "full text. Screening is automated; calls are not investment advice."]
    if bt:
        foot.append(f'<a href="{esc(bt)}" style="color:{MUTED};">Full archive</a>')
    B.append(_rule(1, LINE, "30px 0 12px"))
    B.append(f'<div style="font:11px/1.6 {SANS};color:{MUTED};">'
             f'{" &nbsp;·&nbsp; ".join(foot)}</div>')

    # Outlook 用 Word 渲染引擎，只有 table 的宽度约束它一定认
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f'<title>Podcast Screen {esc(day)}</title></head>'
        f'<body style="margin:0;padding:0;background:#eef0f3;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="background:#eef0f3;"><tr><td align="center" '
        f'style="padding:26px 12px;">'
        f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        f'border="0" style="max-width:640px;width:100%;background:#ffffff;">'
        f'<tr><td style="padding:30px 32px 34px;">{"".join(B)}</td></tr>'
        f'</table></td></tr></table></body></html>')


def render_text(j, idx, day):
    """纯文本备份。不是装饰——只发 HTML 更容易被判成垃圾邮件。"""
    items = j.get("items", [])
    dur = lambda x: _minutes(idx.get(x["episode_id"], {}).get("duration_min"))
    must = [x for x in items if x.get("verdict") == "must_listen"]
    skim = [x for x in items if x.get("verdict") == "worth_skim"]
    skipped = [x for x in items if x.get("verdict") == "skip"]
    keyf = lambda x: (x.get("section") != "subscribed", -(x.get("score") or 0))

    L = [f"PODCAST SCREEN — {day}", "=" * 62, "",
         f"{len(items)} episodes screened · {hhmm(sum(dur(x) for x in items))} "
         f"of audio · {len(must)} flagged to listen · "
         f"{hhmm(sum(dur(x) for x in skipped))} screened out"]
    if headline(j):
        L += ["", "PM SUMMARY", "-" * 62, headline(j)]

    for verdict, group, detailed in (("must_listen", must, True),
                                     ("worth_skim", skim, False),
                                     ("skip", skipped, False)):
        if not group:
            continue
        L += ["", "", f"{SECTION_TITLE[verdict]} ({len(group)})", "-" * 62]
        for x in sorted(group, key=keyf):
            m = idx.get(x["episode_id"], {})
            L += ["", f"[{CALL.get(x.get('verdict'), '')}] {x.get('title')}",
                  f"  {m.get('show', '')} · {hhmm(m.get('duration_min'))}"
                  + (f" · {x['score']}/10" if x.get("score") else ""),
                  f"  {x.get('why', '')}"]
            if detailed:
                L += [f"  - {k}" for k in (x.get("key_points") or [])[:6]]
                if read_across(x):
                    L.append(f"  READ-ACROSS: {read_across(x)}")
            if m.get("link"):
                L.append(f"  {m['link']}")

    if j.get("gaps"):
        L += ["", "", "NOT COVERED", "-" * 62] + [f"- {g}" for g in j["gaps"]]
    L += ["", "", "Screening is automated; calls are not investment advice."]
    return "\n".join(L)


def subject(j, day):
    """收件箱里只看得到这一行，所以必须带数字和最强的那个 call。"""
    items = j.get("items", [])
    must = [x for x in items if x.get("verdict") == "must_listen"]
    s = f"Podcast Screen {day[5:]} — {len(items)} screened, {len(must)} to listen"
    top = max(must, key=lambda x: x.get("score") or 0, default=None)
    if top and top.get("title"):
        s += f" | {top['title'][:46]}"
    return s


# ---------------------------------------------------------------- 发送

def _recipients(raw):
    return [a.strip() for a in (raw or "").replace(";", ",").split(",") if a.strip()]


def send(j, idx, day, cfg=None, dry=False):
    """返回 (ok, 说明)。dry=True 只组装不发送，用来验证配置和渲染。"""
    c = cfg or {}
    host = c.get("host") or os.environ.get("SMTP_HOST", "")
    port = int(c.get("port") or os.environ.get("SMTP_PORT") or 587)
    user = c.get("user") or os.environ.get("SMTP_USER", "")
    pw = c.get("password") or os.environ.get("SMTP_PASS", "")
    sender = c.get("sender") or os.environ.get("EMAIL_FROM") or user
    to = _recipients(c.get("to") or os.environ.get("EMAIL_TO") or user)

    missing = [k for k, v in (("SMTP_HOST", host), ("SMTP_USER", user),
                              ("SMTP_PASS", pw)) if not v]
    if missing:
        return False, f"缺配置：{', '.join(missing)}（见 .env.example）"
    if not to:
        return False, "缺收件人 EMAIL_TO"

    name, addr = parseaddr(sender)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject(j, day), "utf-8")
    msg["From"] = formataddr((str(Header(name or "播客早报", "utf-8")), addr or user))
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(addr or user).split("@")[-1])
    # 同一天重复跑不希望在收件箱里堆成一串，用稳定的 Reference 让客户端归成一个会话
    msg["References"] = f"<podcast-daily-{day}@localhost>"
    msg.attach(MIMEText(render_text(j, idx, day), "plain", "utf-8"))
    msg.attach(MIMEText(render_html(j, idx, day), "html", "utf-8"))

    if dry:
        return True, (f"[dry] 主题「{msg['Subject']}」-> {', '.join(to)} "
                      f"via {host}:{port}，HTML {len(render_html(j, idx, day)):,} 字符")

    ctx = ssl.create_default_context()
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
        else:
            srv = smtplib.SMTP(host, port, timeout=60)
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.ehlo()
        with srv:
            srv.login(user, pw)
            srv.sendmail(addr or user, to, msg.as_string())
        return True, f"已发给 {', '.join(to)}"
    except smtplib.SMTPAuthenticationError as e:
        return False, (f"认证失败（{e.smtp_code}）。Gmail/Outlook 需要用"
                       f"「应用专用密码」而不是登录密码")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:240]}"


def main():
    ap = argparse.ArgumentParser(description="把判断发成 HTML 邮件")
    ap.add_argument("--judgments", default=os.path.join(DATA, "judgments.json"))
    ap.add_argument("--pack", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--preview", metavar="PATH", nargs="?", const="", default=None,
                    help="把 HTML 写到文件预览，不发送（默认 newsletters/ 下）")
    ap.add_argument("--dry-run", action="store_true", help="组装但不发送，验证配置")
    args = ap.parse_args()

    j = load_json(args.judgments, None)
    if not j:
        print(f"读不到 {args.judgments}", file=sys.stderr)
        return 2
    idx = {i["episode_id"]: i for i in (load_json(args.pack, {}) or {}).get("items", [])}
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    if args.preview is not None:
        out = args.preview or os.path.join(ROOT, "newsletters",
                                           f"podcast-daily-{day}.html")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(render_html(j, idx, day))
        print(f"预览已写到 {out}")
        print(f"主题：{subject(j, day)}")
        return 0

    ok, info = send(j, idx, day, dry=args.dry_run)
    print("邮件：" + ("成功 — " if ok else "失败 — ") + info)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
