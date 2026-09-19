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

# 窗口和时效性是两件事，这里刻意分开：
#
#   窗口 = 覆盖率问题。目标是「一集都别漏」，所以由节目**自己的更新节奏**决定。
#   tier = 判断问题。目标是「旧的要不要降级」，交给判断环节（见 digest_pack 的 tier 字段）。
#
# 一开始把 tier 直接当窗口用是错的，实测后果很具体：TCW 90 天只发 2 集、
# Capital Cycle 上一集在 22 天前，两档都被标成高时效 -> 2 天窗口 -> 永远抓不到。
# 「内容多快过期」和「多久发一集」根本不相关：TCW 发得少，但一发就有时效性。
#
# tier 仍然设一个窗口下限，因为长青节目就算更新频繁也值得多看几天。
TIER_MIN_WINDOW = {"fresh": 1.0, "semi": 2.5, "evergreen": 7.0}
DEFAULT_TIER = "fresh"
# 按节奏推窗口的系数：中位间隔 x1.6 + 2 天，保证至少能接住最近一集，
# 又不至于把整季历史都拖进来。
CADENCE_FACTOR, CADENCE_PAD = 1.6, 2.0
MAX_WINDOW_DAYS = 45.0          # 再老的内容即使是长青档也该由人主动去翻，不该每天推


def effective_window(tier, pub_dates, base_days):
    """这档节目该回看多少天。返回 (天数, 判断依据)。"""
    floor = base_days * TIER_MIN_WINDOW.get(tier, 1.0)
    # 按**发布日**去重再算间隔。不去重会被「同一天连发几集」的节目毁掉：
    # Capital Cycle 每次一天发两集（13:30 和 13:31），间隔序列里一半是 0，
    # 中位数被压成 0，窗口算出来 2 天——一档月更节目于是永远抓不到。
    days = sorted({d.date() for d in pub_dates if d}, reverse=True)[:12]
    gaps = [(a - b).days for a, b in zip(days, days[1:]) if (a - b).days > 0]
    if not gaps:
        return min(floor, MAX_WINDOW_DAYS), "无节奏数据"
    gaps.sort()
    med = gaps[len(gaps) // 2]
    cadence = med * CADENCE_FACTOR + CADENCE_PAD
    win = min(max(floor, cadence), MAX_WINDOW_DAYS)
    why = f"中位间隔{med}天" if cadence > floor else f"{tier}下限"
    return win, why

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
    tier_of = {s["display"]: (s.get("tier") or DEFAULT_TIER) for s in shows}
    now = now_utc()
    seen = set(load_json(SEEN_PATH, {}).get("ids", [])) if not args.ignore_seen else set()

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(pull, shows))

    picked, skipped, dropped_deriv, errors, windows = [], [], [], [], []
    for disp, eps, err in results:
        if err:
            errors.append({"show": disp, "error": err})
            continue
        tier = tier_of.get(disp, DEFAULT_TIER)
        win, why = effective_window(
            tier, [parse_date(e["published"]) for e in eps], args.days)
        cutoff = now - timedelta(days=win)
        windows.append({"show": disp, "tier": tier, "days": round(win, 1),
                        "basis": why})
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
            e["tier"] = tier            # 带下去，判断时要知道这集该不该因为旧而降级
            e["age_days"] = (now - pub).days
            fresh.append(e)
        fresh.sort(key=lambda x: x["published"] or "", reverse=True)
        picked.extend(fresh[: args.max_per_show])

    # 一集都没抓到的节目必须点名：静默的零产出和「这档没更新」长得一模一样，
    # 但前者是 bug、后者是事实，不说清楚就没法保证「所有相关内容都抓到了」。
    yielded = {e["show"] for e in picked}
    quiet = []
    for disp, eps, err in results:
        if err or disp in yielded:
            continue
        ds = sorted([parse_date(e["published"]) for e in eps if e["published"]],
                    reverse=True)
        w = next((x for x in windows if x["show"] == disp), {})
        quiet.append({
            "show": disp,
            "last_published": ds[0].strftime("%Y-%m-%d") if ds else None,
            "days_since": (now - ds[0]).days if ds else None,
            "window_days": w.get("days"),
        })

    picked.sort(key=lambda x: x["published"] or "", reverse=True)

    out = {
        "generated_at": now_utc().isoformat(),
        "window_days": args.days,
        "windows": sorted(windows, key=lambda w: -w["days"]),
        "quiet_shows": sorted(quiet, key=lambda q: -(q["days_since"] or 0)),
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
