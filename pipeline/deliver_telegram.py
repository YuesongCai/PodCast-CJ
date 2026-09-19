#!/usr/bin/env python3
"""投递到 Telegram bot。

和飞书那条路径并列，不替代它：推送渠道是转发层，没有存储价值，
所以多一个渠道就是多一份拷贝，哪条挂了都不影响别的（见 deliver.py 的失败隔离）。

三个 Telegram 特有的坑，都在这里处理掉：

1. **单条消息 4096 字符上限**。newsletter 动辄上万字符，必须切块。
   切在「集」的边界上——从中间切会把一集的理由劈成两半，读起来像断了。
2. **parse_mode 用 HTML 而不是 Markdown**。MarkdownV2 要求转义 15 个字符
   （`_*[]()~`>#+-=|{}.!`），播客标题里逗号句号括号全是雷；HTML 只需要转义
   `& < >` 三个，正确性上可控得多。
3. **HTML 模式只认白名单标签**（b/i/u/s/code/pre/a/blockquote），
   `<h2>` 之类会直接报 400，所以排版全靠 b + 换行。

配置（.env）：
    TELEGRAM_BOT_TOKEN=123456:ABC-DEF...     找 @BotFather 要
    TELEGRAM_CHAT_ID=123456789               找 @userinfobot 要，或群 id（负数）
"""
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, load_json

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096
SAFE_LEN = 3900          # 留余量：切块逻辑按段落估算，不想贴着上限走

VERDICT_ICON = {"must_listen": "▲", "worth_skim": "•", "skip": "×"}
CALL = {"must_listen": "LISTEN", "worth_skim": "SKIM", "skip": "SKIP"}
SOURCE_LABEL = {"youtube": "YouTube captions", "asr": "local ASR",
                "rss_inline": "RSS full text", "rss_transcript": "official transcript",
                "website": "show site", "notes_only": "show notes only"}


def esc(s):
    """Telegram HTML 模式只需要转义这三个。顺序重要：& 必须先转。"""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _minutes(v):
    """duration_min 来自各家 RSS，可能是 int、字符串、空值。统一成 int。"""
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return 0


def hhmm(minutes):
    minutes = _minutes(minutes)
    if minutes <= 0:
        return "n/a"
    if minutes < 60:
        return f"{minutes}min"
    return (f"{minutes // 60}h{minutes % 60:02d}m" if minutes % 60
            else f"{minutes // 60}h")


def _headline(j):
    """字段改过名，旧名保留为别名（见 judge.py）。"""
    for k in ("pm_summary", "today_in_one_line"):
        if (j.get(k) or "").strip():
            return j[k].strip()
    return ""


def _debates(j):
    for k in ("debates", "cross_cutting"):
        if isinstance(j.get(k), list):
            return j[k]
    return []


def _read_across(x):
    for k in ("read_across", "for_you"):
        if (x.get(k) or "").strip():
            return x[k].strip()
    return ""


# ---------------------------------------------------------------- 排版

def _blocks(j, idx, day):
    """把判断拆成一串「块」。每块是一段自洽的 HTML，切页只在块之间发生。"""
    items = j.get("items", [])
    must = [x for x in items if x.get("verdict") == "must_listen"]
    skim = [x for x in items if x.get("verdict") == "worth_skim"]
    skipped = [x for x in items if x.get("verdict") == "skip"]

    dur = lambda x: _minutes(idx.get(x["episode_id"], {}).get("duration_min"))
    total = sum(dur(x) for x in items)
    saved = sum(dur(x) for x in skipped)

    out = []
    head = [f"<b>PODCAST SCREEN · {esc(day)}</b>",
            f"{len(items)} screened · {hhmm(total)} of audio · "
            f"<b>{len(must)} to listen</b> · {hhmm(saved)} screened out"]
    if _headline(j):
        head += ["", f"<b>PM SUMMARY</b>", esc(_headline(j))]
    out.append("\n".join(head))

    def one(x, detailed):
        m = idx.get(x["episode_id"], {})
        icon = VERDICT_ICON.get(x.get("verdict"), "•")
        title = esc(x.get("title") or m.get("title"))
        L = [f"{icon} <b>{title}</b>",
             f"{esc(m.get('show') or x.get('show'))} · {hhmm(m.get('duration_min'))}"
             f" · {CALL.get(x.get('verdict'), '')}"
             + (f" {x['score']}/10" if x.get("score") else "")]
        if x.get("why"):
            L.append(f"<i>{esc(x['why'])}</i>")
        if detailed:
            for kp in (x.get("key_points") or [])[:5]:
                L.append(f"· {esc(kp)}")
            if _read_across(x):
                L.append(f"<b>READ-ACROSS</b> {esc(_read_across(x))}")
        links = []
        if m.get("link"):
            links.append(f'<a href="{esc(m["link"])}">原页面</a>')
        if m.get("youtube"):
            links.append(f'<a href="{esc(m["youtube"])}">YouTube</a>')
        tail = " · ".join(links)
        src = SOURCE_LABEL.get(m.get("evidence_source"))
        if src and detailed:
            tail = (tail + " · " if tail else "") + f"<i>Evidence: {src}</i>"
        if m.get("fidelity") == "notes_only":
            tail += " ⚠️ no transcript"
        if tail:
            L.append(tail)
        return "\n".join(L)

    for d in _debates(j):
        cc = [f"<b>KEY DEBATE</b>",
              f"<b>{esc(d.get('question') or d.get('theme'))}</b>"]
        if d.get("conclusion"):
            cc.append(f"<b>Conclusion:</b> {esc(d['conclusion'])}")
        if d.get("detail"):
            cc.append(esc(d["detail"]))
        if d.get("shows"):
            cc.append(f"<i>{esc(' · '.join(d['shows']))}</i>")
        out.append("\n\n".join(cc))

    for label, group, detailed in (("<b>LISTEN</b>", must, True),
                                   ("<b>SKIM</b>", skim, False)):
        if not group:
            continue
        out.append(label)
        # 订阅的排在盲区前面：他自己订的优先级更高
        group = sorted(group, key=lambda x: (x.get("section") != "subscribed",
                                             -(x.get("score") or 0)))
        out.extend(one(x, detailed) for x in group)

    if skipped:
        lines = [f"<b>SCREENED OUT</b> ({len(skipped)} · {hhmm(saved)} saved)"]
        for x in skipped:
            m = idx.get(x["episode_id"], {})
            lines.append(f"× <b>{esc(m.get('show', ''))}</b> — {esc(x.get('title'))} "
                         f"({hhmm(m.get('duration_min'))})\n{esc(x.get('why', ''))}")
        out.append("\n\n".join(lines))

    if j.get("gaps"):
        out.append("<b>NOT COVERED</b>\n"
                   + "\n".join(f"· {esc(g)}" for g in j["gaps"]))

    return out


def _split_long_line(line, limit):
    """切一行超过 limit 的文本，**不丢字符**。

    超长单行意味着某个 key_point 或 why 异常地长。这里先把标签剥成纯文本再切：
    在标签中间切会产生未闭合的 HTML，Telegram 直接返回 400，
    而这条路径本来就是兜底，保住内容比保住加粗重要。

    切点还要避开 HTML 实体——`&amp;` 被从中间切开会变成字面的 `&am`，
    下一段以 `p;` 开头，两段都是坏的。
    """
    plain = re.sub(r"<[^>]+>", "", line)
    out = []
    while len(plain) > limit:
        cut = limit
        # 别切在实体中间：往回找最近的 & ，若它到 cut 之间没有 ; 就退到它前面
        amp = plain.rfind("&", max(0, cut - 12), cut)
        if amp != -1 and ";" not in plain[amp:cut]:
            cut = amp
        # 尽量切在空白处，读起来不至于断在词中间
        sp = plain.rfind(" ", max(1, cut - 80), cut)
        if sp > 0:
            cut = sp
        out.append(plain[:cut].rstrip())
        plain = plain[cut:].lstrip()
    if plain:
        out.append(plain)
    return out


def paginate(blocks, limit=SAFE_LEN):
    """把块合并成不超过 limit 的消息。切页只在块之间，块内超长才按行硬切。"""
    pages, cur = [], ""

    def flush():
        nonlocal cur
        if cur:
            pages.append(cur)
            cur = ""

    for b in blocks:
        if len(b) > limit:
            flush()
            chunk = ""
            lines = []
            for line in b.split("\n"):
                lines.extend([line] if len(line) <= limit
                             else _split_long_line(line, limit))
            for line in lines:
                if len(chunk) + len(line) + 1 > limit:
                    pages.append(chunk)
                    chunk = line
                else:
                    chunk = f"{chunk}\n{line}" if chunk else line
            cur = chunk
            continue
        if len(cur) + len(b) + 2 > limit:
            flush()
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    flush()
    return pages


# ---------------------------------------------------------------- 发送

def _post(token, method, payload, timeout=60):
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _call(token, method, payload, retries=2):
    """带 429 退避的调用。Telegram 限流会在 parameters.retry_after 里给秒数。"""
    for attempt in range(retries + 1):
        try:
            resp = _post(token, method, payload)
            if resp.get("ok"):
                return True, resp
            return False, resp.get("description", str(resp)[:200])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                info = json.loads(body)
            except json.JSONDecodeError:
                info = {}
            if e.code == 429 and attempt < retries:
                time.sleep(info.get("parameters", {}).get("retry_after", 3) + 1)
                continue
            return False, f"HTTP {e.code}: {info.get('description', body[:200])}"
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return False, f"{type(e).__name__}: {str(e)[:200]}"
    return False, "重试耗尽"


def send_document(token, chat_id, path, caption="", timeout=180):
    """multipart 上传 md 全文。Telegram 没有 JSON 版的文件上传。"""
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode("utf-8")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{os.path.basename(path)}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    body += content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        API.format(token=token, method="sendDocument"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        return (True, "") if resp.get("ok") else (False, str(resp.get("description"))[:200])
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def send(j, idx, day, md_path=None, token=None, chat_id=None):
    """返回 (ok, 说明)。任一分页失败即整体判失败，但已发出的不回滚。"""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False, "未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（见 .env.example）"

    pages = paginate(_blocks(j, idx, day))
    errs = []
    for n, text in enumerate(pages, 1):
        ok, info = _call(token, "sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if not ok:
            errs.append(f"第 {n}/{len(pages)} 条失败: {info}")
        elif n < len(pages):
            time.sleep(0.4)          # Telegram 同一 chat 约 1 条/秒，别撞限流

    if md_path and os.path.exists(md_path) and not errs:
        ok, info = send_document(token, chat_id, md_path, caption=f"Podcast Screen {day} — full note")
        if not ok:
            errs.append(f"md 附件失败: {info}")

    return (not errs), ("; ".join(errs) if errs else f"{len(pages)} 条消息")


def main():
    import argparse
    from datetime import datetime, timedelta, timezone
    ap = argparse.ArgumentParser(description="把判断推到 Telegram")
    ap.add_argument("--judgments", default=os.path.join(DATA, "judgments.json"))
    ap.add_argument("--pack", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--md", default=None, help="同时上传的 md 全文")
    ap.add_argument("--preview", action="store_true", help="只打印分页，不发送")
    args = ap.parse_args()

    j = load_json(args.judgments, None)
    if not j:
        print(f"读不到 {args.judgments}", file=sys.stderr)
        return 2
    idx = {i["episode_id"]: i for i in (load_json(args.pack, {}) or {}).get("items", [])}
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    if args.preview:
        pages = paginate(_blocks(j, idx, day))
        for n, p in enumerate(pages, 1):
            print(f"\n{'=' * 20} 第 {n}/{len(pages)} 条（{len(p)} 字符）{'=' * 20}\n{p}")
        return 0

    ok, info = send(j, idx, day, args.md)
    print("Telegram: " + ("sent — " if ok else "failed — ") + info)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
