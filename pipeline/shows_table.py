#!/usr/bin/env python3
"""把订阅列表落成一张可核对的表（Markdown / CSV）。

两个用途，是同一张表：

1. **录入核对**。给一份 podcast list 进来之后，节目名 -> RSS 是靠 iTunes 搜索猜的，
   会猜错。同名节目、改过名的节目、被播客托管商克隆的马甲 feed，都能拿到高分。
   这张表把「你给的名字」和「实际订到的 feed」并排放，错的一眼能看出来。
   匹配分 <80 的行会标 ⚠️，那是需要人看一眼的。

2. **盘点**。哪档多久更一次、notes 厚不厚、有没有官方 transcript——
   决定了这档节目在流水线里能拿到什么质量的证据。

用法：
    python3 pipeline/shows_table.py                      # 打印 Markdown
    python3 pipeline/shows_table.py --csv out.csv        # 另存 CSV
    python3 pipeline/shows_table.py --watchlist          # 看盲区列表
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, load_json


def cadence(h):
    """把更新频率说成人话。看 30 天实际集数，比看 avg_days_between 稳。"""
    if not h:
        return "—"
    n30 = h.get("eps_last_30d")
    if not n30:
        return "近 30 天无更新"
    if n30 >= 20:
        return f"几乎每天（30天 {n30} 集）"
    if n30 >= 8:
        return f"每周多集（30天 {n30} 集）"
    if n30 >= 3:
        return f"约每周（30天 {n30} 集）"
    return f"偶发（30天 {n30} 集）"


def transcript_note(h):
    if not h:
        return "—"
    r = h.get("transcript_tag_ratio") or 0
    if r >= 0.5:
        return f"✅ 官方 {r:.0%}"
    if r > 0:
        return f"部分 {r:.0%}"
    med = h.get("notes_chars_median") or 0
    return f"无官方（notes {med:,} 字符）" if med else "无官方"


def build(feeds, health, seed_names):
    hx = {h.get("display"): h for h in (health or [])}
    rows = []
    for f in feeds:
        name = f.get("display")
        h = hx.get(name)
        score = f.get("match_score")
        resolved = f.get("itunes_name") or ""
        # 名字对不上 or 分数低 -> 要人看
        suspect = (score is not None and score < 80) or not f.get("feed")
        rows.append({
            "你给的名字": name,
            "实际订到": resolved or "❌ 没解析到",
            "作者": f.get("itunes_author") or f.get("author") or "",
            "更新频率": cadence(h),
            "官方 transcript": transcript_note(h),
            "最近更新": (h or {}).get("latest") or (f.get("last_release") or "")[:10],
            "匹配分": score if score is not None else "",
            "需核对": "⚠️" if suspect else "",
            "feed": f.get("feed") or "",
        })
    # 需核对的排最前面，省得埋在中间
    rows.sort(key=lambda r: (r["需核对"] != "⚠️", r["你给的名字"].lower()))

    missing = [n for n in seed_names if n not in {f.get("display") for f in feeds}]
    return rows, missing


def to_markdown(rows, title):
    cols = ["你给的名字", "实际订到", "作者", "更新频率", "官方 transcript",
            "最近更新", "匹配分", "需核对"]
    L = [f"## {title}（{len(rows)} 档）", "",
         "| " + " | ".join(cols) + " |",
         "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        L.append("| " + " | ".join(
            str(r[c]).replace("|", "\\|") for c in cols) + " |")
    warn = sum(1 for r in rows if r["需核对"])
    L += ["", f"> ⚠️ {warn} 档需要人看一眼（匹配分 <80 或没解析到 feed）。"
              f"确认方式：点开 feed 看前几集标题对不对得上。" if warn
          else "> 全部匹配良好。"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="订阅列表核对表")
    ap.add_argument("--watchlist", action="store_true", help="看盲区 watchlist")
    ap.add_argument("--csv", metavar="PATH", help="另存 CSV")
    ap.add_argument("--out", metavar="PATH", help="另存 Markdown")
    args = ap.parse_args()

    if args.watchlist:
        feeds_p = os.path.join(DATA, "watchlist_feeds.json")
        seed_p = os.path.join(DATA, "watchlist_seed.txt")
        title = "盲区 watchlist"
    else:
        feeds_p = os.path.join(DATA, "feeds.json")
        seed_p = os.path.join(DATA, "shows_seed.txt")
        title = "订阅列表"

    feeds = load_json(feeds_p, None)
    if not feeds:
        print(f"读不到 {feeds_p}。先跑：\n"
              f"  python3 pipeline/resolve_feeds.py {seed_p} {feeds_p}",
              file=sys.stderr)
        return 2
    health = load_json(os.path.join(DATA, "feed_health.json"), [])

    seed_names = []
    if os.path.exists(seed_p):
        with open(seed_p, encoding="utf-8") as f:
            seed_names = [l.split("|")[0].strip() for l in f
                          if l.strip() and not l.startswith("#")]

    rows, missing = build(feeds, health, seed_names)
    md = to_markdown(rows, title)
    if missing:
        md += (f"\n\n> 另有 {len(missing)} 档在 seed 里但没出现在解析结果，"
               f"重跑 resolve_feeds.py：{'、'.join(missing)}")
    print(md)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"\n-> {args.out}", file=sys.stderr)
    if args.csv:
        cols = list(rows[0].keys()) if rows else []
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"-> {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
