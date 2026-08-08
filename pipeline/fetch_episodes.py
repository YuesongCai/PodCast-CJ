#!/usr/bin/env python3
"""拉取所有订阅 feed 在时间窗内的新集，去重后写成 episodes JSON。

带 seen 状态文件，跑第二遍不会重复交付同一集。

用法：
    python3 pipeline/fetch_episodes.py --days 2
    python3 pipeline/fetch_episodes.py --days 7 --ignore-seen   # 补跑/测试
"""
import argparse
import re
import os
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (DATA, NS, ep_id, http_get, load_json, now_utc, parse_date,
                    parse_duration, save_json, strip_html)

SEEN_PATH = os.path.join(DATA, "seen_episodes.json")

# 派生集：内容和正片重复，进 newsletter 只会占版面。
DERIVATIVE_RE = re.compile(
    r"^\s*(highlights?|trailer|preview|teaser|encore|replay|rerun|best of|"
    r"coming soon|introducing|bonus:\s*trailer)\b|"
    r"\b(highlights?)\s*:|\(\s*(trailer|preview|replay|encore)\s*\)", re.I)

# 机构研报播客的 show notes 尾部固定免责声明，占了正文一大半，先切掉。
DISCLAIMER_RE = re.compile(
    r"(this (podcast|material|recording) (is|has been)|"
    r"the views expressed|for important disclosures|"
    r"past performance is (not|no)|"
    r"this (information|content) is (for|intended)|"
    r"©\s*20\d\d|all rights reserved|"
    r"is a registered broker-dealer|member of finra|"
    r"nothing (in this|herein) (podcast |)(should be|constitutes)|"
    r"not (an offer|intended as investment advice)|"
    r"please see .{0,40}disclosure)", re.I)


def is_derivative(title):
    return bool(DERIVATIVE_RE.search(title or ""))


def trim_disclaimer(notes):
    """从最早出现的免责声明处截断，但至少保留 200 字符，避免误杀短简介。"""
    if not notes:
        return notes
    m = DISCLAIMER_RE.search(notes)
    if m and m.start() >= 200:
        return notes[: m.start()].rstrip()
    return notes


def t(el, path):
    if el is None:
        return ""
    x = el.find(path, NS)
    return (x.text or "").strip() if x is not None and x.text else ""


def pull(show):
    """拉单个 feed，返回 (show_display, [episodes], error)。"""
    disp, feed = show["display"], show.get("feed")
    if not feed:
        return disp, [], show.get("blocked") or "no_feed"
    try:
        raw = http_get(feed)
        root = ET.fromstring(raw)
    except Exception as e:
        return disp, [], f"fetch/parse: {str(e)[:120]}"

    chan = root.find("channel")
    if chan is None:
        return disp, [], "not_rss"

    show_title = t(chan, "title") or disp
    show_link = t(chan, "link")
    eps = []
    for it in chan.findall("item"):
        pub = parse_date(t(it, "pubDate"))
        title = t(it, "title")
        if not title:
            continue

        # 音频地址
        audio, audio_len, audio_type = None, None, None
        for enc in it.findall("enclosure"):
            u = enc.attrib.get("url")
            if u:
                audio = u
                audio_len = enc.attrib.get("length")
                audio_type = enc.attrib.get("type")
                break

        # 描述：优先 content:encoded（Substack 类会塞全文）
        desc_html = (t(it, "content:encoded") or t(it, "description")
                     or t(it, "itunes:summary"))
        notes_raw = strip_html(desc_html)
        notes = trim_disclaimer(notes_raw)

        tx = [{"url": x.attrib.get("url"), "type": x.attrib.get("type")}
              for x in it.findall("podcast:transcript", NS) if x.attrib.get("url")]

        guid = t(it, "guid")
        eps.append({
            "id": ep_id(disp, guid, title),
            "show": disp,
            "show_title": show_title,
            "show_link": show_link,
            "title": title,
            "guid": guid,
            "published": pub.isoformat() if pub else None,
            "published_date": pub.strftime("%Y-%m-%d") if pub else None,
            "duration_sec": parse_duration(t(it, "itunes:duration")),
            "link": t(it, "link"),
            "audio_url": audio,
            "audio_bytes": int(audio_len) if (audio_len or "").isdigit() else None,
            "audio_type": audio_type,
            "episode_type": t(it, "itunes:episodeType"),
            "notes": notes,
            "notes_chars": len(notes),
            "notes_trimmed_chars": len(notes_raw) - len(notes),
            "derivative": is_derivative(title),
            "rss_transcripts": tx,
        })
    return disp, eps, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=2, help="回看窗口（天）")
    ap.add_argument("--feeds", default=os.path.join(DATA, "feeds.json"))
    ap.add_argument("--out", default=os.path.join(DATA, "episodes.json"))
    ap.add_argument("--ignore-seen", action="store_true", help="不过滤历史已交付的集")
    ap.add_argument("--no-mark", action="store_true", help="不写入 seen 状态（干跑）")
    ap.add_argument("--max-per-show", type=int, default=6, help="单档最多取几集，防某档刷屏")
    ap.add_argument("--keep-derivative", action="store_true",
                    help="保留 highlights/预告/重播（默认丢弃，因为和正片重复）")
    args = ap.parse_args()

    shows = load_json(args.feeds, [])
    cutoff = now_utc() - timedelta(days=args.days)
    seen = set(load_json(SEEN_PATH, {}).get("ids", [])) if not args.ignore_seen else set()

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(pull, shows))

    picked, skipped, dropped_deriv, errors = [], [], [], []
    for disp, eps, err in results:
        if err:
            errors.append({"show": disp, "error": err})
            continue
        fresh = []
        for e in eps:
            pub = parse_date(e["published"]) if e["published"] else None
            if not pub or pub < cutoff:
                continue
            if e["id"] in seen:
                skipped.append(e["title"][:60])
                continue
            if e["derivative"] and not args.keep_derivative:
                dropped_deriv.append(f"{disp} / {e['title'][:50]}")
                continue
            fresh.append(e)
        fresh.sort(key=lambda x: x["published"] or "", reverse=True)
        picked.extend(fresh[: args.max_per_show])

    picked.sort(key=lambda x: x["published"] or "", reverse=True)

    out = {
        "generated_at": now_utc().isoformat(),
        "window_days": args.days,
        "cutoff": cutoff.isoformat(),
        "shows_total": len(shows),
        "shows_ok": sum(1 for _, _, e in results if not e),
        "episode_count": len(picked),
        "errors": errors,
        "episodes": picked,
    }
    save_json(args.out, out)

    print(f"窗口 {args.days} 天（{cutoff:%Y-%m-%d %H:%M} UTC 起）")
    print(f"feed {out['shows_ok']}/{len(shows)} 正常，抓到新集 {len(picked)} 条"
          f"（历史已交付跳过 {len(skipped)} 条，派生集丢弃 {len(dropped_deriv)} 条）")
    if dropped_deriv:
        print("丢弃的派生集（highlights/预告/重播，与正片重复）：")
        for x in dropped_deriv:
            print(f"  - {x}")
    if errors:
        print("\n取不到的节目：")
        for e in errors:
            print(f"  - {e['show']}: {e['error']}")
    print(f"\n{'节目':30} {'日期':11} {'时长':>6} {'notes':>6}  标题")
    print("-" * 108)
    for e in picked:
        mins = f"{e['duration_sec']//60}m" if e["duration_sec"] else "-"
        print(f"{e['show'][:29]:30} {e['published_date'] or '-':11} {mins:>6} "
              f"{e['notes_chars']:>6}  {e['title'][:48]}")

    if not args.no_mark and not args.ignore_seen:
        ids = sorted(seen | {e["id"] for e in picked})
        save_json(SEEN_PATH, {"ids": ids[-4000:]})
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    main()
