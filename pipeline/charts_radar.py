#!/usr/bin/env python3
"""榜单雷达：扫 Apple 榜单，找出既不在订阅列表、也不在 watchlist 里的高排名节目。

定位是「提示你可能该把某档加进 watchlist」，不是直接产出每日内容。
原因：榜单优化的是大众流行度。Business 榜前列长期被励志/带货类占据，
对做投资的人是噪音。所以这里只输出候选+排名，由人决定要不要纳入。

数据源（都免费、无需 key）：
  - https://rss.marketingtools.apple.com/api/v2/us/podcasts/top/{n}/podcasts.json
  - https://itunes.apple.com/us/rss/toppodcasts/limit={n}/genre={id}/json

用法：
    python3 pipeline/charts_radar.py
    python3 pipeline/charts_radar.py --limit 100
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, http_json, load_json, now_utc, save_json
from get_transcript import norm_title

# Apple 类目 ID
GENRES = {
    1321: "Business",
    1318: "Technology",
    1489: "News",
    1493: "Business/Investing" ,
}


def top_overall(limit):
    url = f"https://rss.marketingtools.apple.com/api/v2/us/podcasts/top/{limit}/podcasts.json"
    try:
        d = http_json(url)
    except Exception as e:
        return [], str(e)[:100]
    out = []
    for i, r in enumerate(d.get("feed", {}).get("results", []), 1):
        out.append({"rank": i, "chart": "Top Shows (overall)", "name": r.get("name"),
                    "author": r.get("artistName"), "itunes_id": r.get("id"),
                    "url": r.get("url")})
    return out, None


def top_genre(gid, name, limit):
    url = f"https://itunes.apple.com/us/rss/toppodcasts/limit={limit}/genre={gid}/json"
    try:
        d = http_json(url)
    except Exception as e:
        return [], str(e)[:100]
    out = []
    for i, e in enumerate(d.get("feed", {}).get("entry", []) or [], 1):
        try:
            nm = e["im:name"]["label"]
            au = e.get("im:artist", {}).get("label")
            iid = e.get("id", {}).get("attributes", {}).get("im:id")
            link = e.get("id", {}).get("label")
        except Exception:
            continue
        out.append({"rank": i, "chart": name, "name": nm, "author": au,
                    "itunes_id": iid, "url": link})
    return out, None


def known_keys():
    """已订阅 + 已在 watchlist 的节目，用于排除。"""
    keys, labels = set(), {}
    for path in ("feeds.json", "watchlist_feeds.json"):
        for r in load_json(os.path.join(DATA, path), []) or []:
            for nm in (r.get("display"), r.get("itunes_name")):
                if nm:
                    keys.add(norm_title(nm))
                    labels[norm_title(nm)] = r.get("display")
            if r.get("itunes_id"):
                keys.add(f"id:{r['itunes_id']}")
    return keys, labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--out", default=os.path.join(DATA, "charts_radar.json"))
    args = ap.parse_args()

    known, _ = known_keys()
    rows, errs = [], []

    r, e = top_overall(args.limit)
    rows += r
    if e:
        errs.append({"chart": "overall", "error": e})
    for gid, gname in GENRES.items():
        r, e = top_genre(gid, gname, args.limit)
        rows += r
        if e:
            errs.append({"chart": gname, "error": e})

    # 去重（同一档可能上多个榜），保留最好排名
    best = {}
    for x in rows:
        k = f"id:{x['itunes_id']}" if x.get("itunes_id") else norm_title(x["name"])
        if k not in best or x["rank"] < best[k]["rank"]:
            best[k] = x
        else:
            best[k].setdefault("also_on", []).append(f"{x['chart']}#{x['rank']}")

    new, already = [], []
    for k, x in best.items():
        is_known = (k in known) or (norm_title(x["name"]) in known) or \
                   (x.get("itunes_id") and f"id:{x['itunes_id']}" in known)
        (already if is_known else new).append(x)

    new.sort(key=lambda x: x["rank"])
    already.sort(key=lambda x: x["rank"])

    print(f"榜单共 {len(best)} 档去重后节目；已在订阅/watchlist 内 {len(already)} 档，"
          f"未覆盖 {len(new)} 档")
    if already:
        print("\n已覆盖（说明列表和主流榜单有重叠，是好事）：")
        for x in already[:12]:
            print(f"  {x['chart'][:22]:23} #{x['rank']:<3} {x['name'][:46]}")
    print(f"\n未覆盖的高排名节目（供 CJ 决定是否加进 watchlist）：")
    print(f"{'榜单':24} {'排名':>4}  {'节目':46} 作者")
    print("-" * 104)
    for x in new[:40]:
        print(f"{x['chart'][:23]:24} {x['rank']:>4}  {x['name'][:45]:46} {(x['author'] or '')[:26]}")

    save_json(args.out, {"generated_at": now_utc().isoformat(), "errors": errs,
                         "uncovered": new, "already_covered": already})
    print(f"\n写入 {args.out}")
    if errs:
        print("榜单取数报错：", errs)


if __name__ == "__main__":
    main()
