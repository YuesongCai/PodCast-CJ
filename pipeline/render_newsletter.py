#!/usr/bin/env python3
"""把 Claude 产出的判断（judgments.json）渲染成 newsletter，并推送到飞书。

分工很明确：
  - build_digest.py 负责把正文压成证据包（纯代码，零成本）
  - Claude 读证据包，产出 judgments.json（只做筛选和判断）
  - 本脚本负责排版和投递（纯代码）

judgments.json 结构见 prompts/daily_digest_prompt.md。

用法：
    python3 pipeline/render_newsletter.py                       # 只生成 md
    python3 pipeline/render_newsletter.py --send                # 生成并发飞书
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, ROOT, load_json, now_utc

LARK_CLI = os.environ.get("LARK_CLI", "/opt/homebrew/bin/lark-cli")

# 凭据全部走环境变量或 .env——仓库是公开的，webhook URL 一旦泄漏
# 任何人都能往群里发消息。见 .env.example。
LARK_USER = os.environ.get("LARK_USER_ID", "")
# 群机器人 webhook。webhook 只能发消息、不能传文件，
# 所以群里发交互卡片（正文摘要），文件仍走 lark-cli 私聊。
LARK_WEBHOOK = os.environ.get("LARK_WEBHOOK", "")

VERDICT_ICON = {"must_listen": "▲", "worth_skim": "•", "skip": "×"}
CALL = {"must_listen": "LISTEN", "worth_skim": "SKIM", "skip": "SKIP"}
SECTION_TITLE = {"must_listen": "LISTEN", "worth_skim": "SKIM",
                 "skip": "SCREENED OUT"}
SOURCE_LABEL = {"youtube": "YouTube captions", "asr": "local ASR",
                "rss_inline": "RSS full text", "rss_transcript": "official transcript",
                "website": "show site", "notes_only": "show notes only"}
FIDELITY_NOTE = {
    "full": "",
    "partial": "(partial transcript — call made conservatively)",
    "notes_only": "(**no transcript** — call made on the description only)",
}


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


def headline(j):
    """字段改过名，旧名保留为别名（见 judge.py）。"""
    for k in ("pm_summary", "today_in_one_line"):
        if (j.get(k) or "").strip():
            return j[k].strip()
    return ""


def debates(j):
    for k in ("debates", "cross_cutting"):
        if isinstance(j.get(k), list):
            return j[k]
    return []


def read_across(x):
    for k in ("read_across", "for_you"):
        if (x.get(k) or "").strip():
            return x[k].strip()
    return ""


def render(j, pack_index):
    """j: judgments dict; pack_index: episode_id -> pack item（补链接/时长等元信息）。

    产出是留档用的 markdown 全文，也是飞书/Telegram 的附件。
    语体和邮件一致：英文、结论先行、研报分栏。
    """
    tz = timezone(timedelta(hours=8))
    today = now_utc().astimezone(tz)
    L = []

    items = j.get("items", [])
    dur = lambda x: _minutes(pack_index.get(x["episode_id"], {}).get("duration_min"))
    must = [x for x in items if x.get("verdict") == "must_listen"]
    skim = [x for x in items if x.get("verdict") == "worth_skim"]
    skipped = [x for x in items if x.get("verdict") == "skip"]
    n_sub = sum(1 for x in items if x.get("section") == "subscribed")
    total, saved = sum(dur(x) for x in items), sum(dur(x) for x in skipped)
    # 订阅的排前面：他自己订的优先级高于盲区补充
    keyf = lambda x: (x.get("section") != "subscribed", -(x.get("score") or 0))

    L += [f"# Podcast Screen — {today:%Y-%m-%d, %A}", "",
          f"> **{len(items)}** episodes screened ({n_sub} subscribed / "
          f"{len(items) - n_sub} blind-spot) · **{hhmm(total)}** of audio · "
          f"**{len(must)} flagged to listen** · {hhmm(saved)} screened out", ""]

    if headline(j):
        L += ["## PM Summary", "", headline(j), ""]

    if debates(j):
        L += ["## Key Debates", ""]
        for d in debates(j):
            L.append(f"### {d.get('question') or d.get('theme')}")
            L.append("")
            if d.get("conclusion"):
                L += [f"**Conclusion:** {d['conclusion']}", ""]
            if d.get("detail"):
                L += [d["detail"], ""]
            if d.get("shows"):
                L += [f"<sub>{' · '.join(d['shows'])}</sub>", ""]

    def block(x, detailed):
        meta = pack_index.get(x["episode_id"], {})
        v = x.get("verdict", "worth_skim")
        L.append(f"### {VERDICT_ICON.get(v, '•')} {x.get('title') or meta.get('title')}")
        L.append("")
        L.append(f"**{meta.get('show') or x.get('show')}** · "
                 f"{hhmm(meta.get('duration_min'))} · **{CALL.get(v, v)}**"
                 + (f" {x['score']}/10" if x.get("score") else ""))
        L.append("")
        if x.get("why"):
            L.extend([x["why"], ""])
        if detailed:
            for kp in (x.get("key_points") or []):
                L.append(f"- {kp}")
            if x.get("key_points"):
                L.append("")
            if read_across(x):
                L.extend([f"**Read-across:** {read_across(x)}", ""])
        links = []
        if meta.get("link"):
            links.append(f"[Episode]({meta['link']})")
        if meta.get("youtube"):
            links.append(f"[YouTube]({meta['youtube']})")
        src = SOURCE_LABEL.get(meta.get("evidence_source"), "—")
        fid = FIDELITY_NOTE.get(meta.get("fidelity", "full"), "")
        L.append(" · ".join(links + [f"<sub>Evidence: {src}</sub>"])
                 + (f" {fid}" if fid else ""))
        L.append("")

    for verdict, group, detailed in (("must_listen", must, True),
                                     ("worth_skim", skim, False)):
        if not group:
            continue
        L += ["---", "", f"## {SECTION_TITLE[verdict]} ({len(group)} of {len(items)})", ""]
        for x in sorted(group, key=keyf):
            block(x, detailed)

    if skipped:
        L += ["---", "",
              f"## Screened out ({len(skipped)} episodes · {hhmm(saved)} saved)", ""]
        for x in sorted(skipped, key=keyf):
            m = pack_index.get(x["episode_id"], {})
            L += [f"- **{m.get('show', '')}** — {x.get('title')} "
                  f"({hhmm(m.get('duration_min'))}) — {x.get('why', '')}"]
        L.append("")

    if j.get("gaps"):
        L += ["---", "", "## Not covered", ""]
        L += [f"- {g}" for g in j["gaps"]]
        L.append("")

    L += ["---",
          f"<sub>Generated {today:%Y-%m-%d %H:%M} (UTC+8) · transcripts from YouTube "
          f"captions, local ASR, and publisher RSS full text · screening is automated; "
          f"calls are not investment advice.</sub>"]
    return "\n".join(L)


def send_webhook(j, idx, day, md_path):
    """发到群机器人 webhook。用交互卡片，正文直接进群，不依赖附件。"""
    import json as _json
    import urllib.request

    items = j.get("items", [])
    must = [x for x in items if x.get("verdict") == "must_listen"]
    skim = [x for x in items if x.get("verdict") == "worth_skim"]
    skipped = [x for x in items if x.get("verdict") == "skip"]
    total_min = sum((idx.get(x["episode_id"], {}).get("duration_min") or 0) for x in items)
    saved_min = sum((idx.get(x["episode_id"], {}).get("duration_min") or 0) for x in skipped)

    el = []
    el.append({"tag": "div", "text": {"tag": "lark_md", "content":
               f"**{len(items)} 集 · {hhmm(total_min)} 音频 · {len(must)} 集建议听**\n"
               f"帮你省掉 {hhmm(saved_min)} 不必听的"}})
    if j.get("today_in_one_line"):
        el.append({"tag": "div", "text": {"tag": "lark_md",
                   "content": f"💡 {j['today_in_one_line']}"}})
    el.append({"tag": "hr"})

    for x in must:
        m = idx.get(x["episode_id"], {})
        links = []
        if m.get("link"):
            links.append(f"[原页面]({m['link']})")
        if m.get("youtube"):
            links.append(f"[YouTube]({m['youtube']})")
        kp = "\n".join(f"· {k}" for k in (x.get("key_points") or [])[:4])
        body = (f"**🎧 {x.get('title')}**\n"
                f"{m.get('show','')} · {hhmm(m.get('duration_min'))} · {x.get('score')}/10\n"
                f"*{x.get('why','')}*\n{kp}")
        if links:
            body += "\n" + " · ".join(links)
        el.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
        el.append({"tag": "hr"})

    if skim:
        lines = []
        for x in skim:
            m = idx.get(x["episode_id"], {})
            lines.append(f"**👀 {x.get('title')}**\n"
                         f"{m.get('show','')} · {hhmm(m.get('duration_min'))} — {x.get('why','')}")
        el.append({"tag": "div", "text": {"tag": "lark_md",
                   "content": "**扫一眼就够**\n\n" + "\n\n".join(lines)}})
        el.append({"tag": "hr"})

    if skipped:
        lines = [f"· {idx.get(x['episode_id'],{}).get('show','')}｜{x.get('title')}"
                 f"（{hhmm(idx.get(x['episode_id'],{}).get('duration_min'))}）— {x.get('why','')}"
                 for x in skipped]
        el.append({"tag": "div", "text": {"tag": "lark_md",
                   "content": "**⏭️ 今天替你跳过的**\n" + "\n".join(lines)}})

    if j.get("cross_cutting"):
        cc = "\n\n".join(f"**{c.get('theme')}**\n{c.get('detail')}"
                         for c in j["cross_cutting"])
        el.append({"tag": "hr"})
        el.append({"tag": "div", "text": {"tag": "lark_md", "content": "🔀 **交叉主题**\n\n" + cc}})

    if j.get("gaps"):
        el.append({"tag": "hr"})
        el.append({"tag": "div", "text": {"tag": "lark_md", "content":
                   "⚠️ **没能覆盖的**\n" + "\n".join(f"· {g}" for g in j["gaps"])}})

    # 多维表格入口：群里要能直接点进去筛历史，不然每天推完就沉底了
    bt = (load_json(os.path.join(DATA, "bitable.json"), {}) or {}).get("url")
    if bt:
        srcs = {}
        for x in items:
            s = idx.get(x["episode_id"], {}).get("evidence_source")
            srcs[s] = srcs.get(s, 0) + 1
        名 = {"youtube": "YouTube 字幕", "asr": "本地转录", "rss_transcript": "官方 transcript",
              "rss_inline": "RSS 全文", "website": "官网正文", "notes_only": "仅简介"}
        dist = "、".join(f"{名.get(k, k)} {v}" for k, v in
                        sorted(srcs.items(), key=lambda kv: -kv[1]))
        full = sum(1 for x in items
                   if idx.get(x["episode_id"], {}).get("fidelity") == "full")
        el.append({"tag": "hr"})
        el.append({"tag": "div", "text": {"tag": "lark_md", "content":
                   f"📊 **[全部判断存档（多维表格）]({bt})**\n"
                   f"可按判定、主题标签、节目、正文来源筛选和累积\n"
                   f"<font color='grey'>本期正文覆盖 {full}/{len(items)} 集 · {dist}</font>"}})

    el.append({"tag": "note", "elements": [{"tag": "plain_text",
               "content": f"完整版 {os.path.basename(md_path)}"}]})

    card = {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text",
                   "content": f"📻 播客早报 · {day}"}},
        "elements": el}}

    req = urllib.request.Request(
        LARK_WEBHOOK, data=_json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = _json.loads(r.read().decode("utf-8", "replace"))
        if resp.get("code") in (0, None) or resp.get("StatusCode") == 0:
            return True, ""
        return False, str(resp)[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def send_lark(md_path, summary):
    if not os.path.exists(LARK_CLI):
        return False, f"找不到 {LARK_CLI}"
    ok, errs = True, []
    r = subprocess.run([LARK_CLI, "im", "+messages-send", "--as", "bot",
                        "--user-id", LARK_USER, "--markdown", summary],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        ok = False
        errs.append(f"摘要发送失败: {(r.stderr or r.stdout)[-200:]}")
    # --file 只接受相对当前工作目录的路径
    d, fn = os.path.dirname(md_path), os.path.basename(md_path)
    r2 = subprocess.run([LARK_CLI, "im", "+messages-send", "--as", "bot",
                         "--user-id", LARK_USER, "--file", f"./{fn}"],
                        capture_output=True, text=True, timeout=180, cwd=d)
    if r2.returncode != 0:
        ok = False
        errs.append(f"文件发送失败: {(r2.stderr or r2.stdout)[-200:]}")
    return ok, "; ".join(errs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judgments", default=os.path.join(DATA, "judgments.json"))
    ap.add_argument("--pack", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--send", action="store_true", help="私聊推送（含 md 附件）")
    ap.add_argument("--webhook", action="store_true", help="推送到群 webhook（交互卡片）")
    args = ap.parse_args()

    j = load_json(args.judgments, None)
    if not j:
        print(f"读不到 {args.judgments}——需要先让 Claude 按 "
              f"prompts/daily_digest_prompt.md 产出判断", file=sys.stderr)
        return 2
    pack = load_json(args.pack, {}) or {}
    idx = {i["episode_id"]: i for i in pack.get("items", [])}

    md = render(j, idx)
    tz = timezone(timedelta(hours=8))
    day = now_utc().astimezone(tz).strftime("%Y-%m-%d")
    out = args.out or os.path.join(ROOT, "newsletters", f"podcast-daily-{day}.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)

    items = j.get("items", [])
    must = [x for x in items if x.get("verdict") == "must_listen"]
    print(f"已生成 {out}")
    print(f"  {len(items)} 集，其中 {len(must)} 集建议听")

    if args.webhook:
        ok, err = send_webhook(j, idx, day, out)
        print("群 webhook 推送：" + ("成功" if ok else f"失败 — {err}"))

    if args.send:
        lines = [f"📻 **播客早报 {day}** — {len(items)} 集，{len(must)} 集建议听"]
        if j.get("today_in_one_line"):
            lines.append(f"\n{j['today_in_one_line']}")
        if must:
            lines.append("\n**今天值得听的：**")
            for x in must[:5]:
                m = idx.get(x["episode_id"], {})
                lines.append(f"· {m.get('show','')}｜{(x.get('title') or '')[:44]}"
                             f"（{hhmm(m.get('duration_min'))}）")
        lines.append("\n完整版见附件 ↓")
        ok, err = send_lark(out, "\n".join(lines))
        print("飞书推送：" + ("成功" if ok else f"失败 — {err}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
