#!/usr/bin/env python3
"""把节目名解析成 RSS feed URL。数据源：iTunes Search API（免费、无需 key）。

用法：
    python3 pipeline/resolve_feeds.py data/shows_seed.txt data/feeds.json
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 podcast-newsletter/0.1"


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def norm(s):
    """归一化用于匹配：小写、去标点、压空格。"""
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def score(cand, want_title, want_author):
    """候选与目标的匹配度。标题权重高于作者。"""
    ct, ca = norm(cand.get("collectionName", "")), norm(cand.get("artistName", ""))
    wt, wa = norm(want_title), norm(want_author)
    s = 0
    if ct == wt:
        s += 100
    elif wt and (wt in ct or ct.startswith(wt)):
        s += 60
    elif wt and ct and ct in wt:
        s += 40
    else:
        # 逐词重叠
        tw, cw = set(wt.split()), set(ct.split())
        if tw:
            s += 30 * len(tw & cw) / len(tw)
    if wa and ca:
        if wa == ca:
            s += 40
        elif wa in ca or ca in wa:
            s += 25
        else:
            aw, caw = set(wa.split()), set(ca.split())
            if aw:
                s += 15 * len(aw & caw) / len(aw)
    if not cand.get("feedUrl"):
        s -= 1000
    return s


def resolve(title, author, query_override):
    term = query_override or f"{title} {author}"
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"media": "podcast", "entity": "podcast", "limit": 12, "term": term}
    )
    try:
        data = http_json(url)
    except Exception as e:
        return None, f"search failed: {e}", []
    results = [r for r in data.get("results", []) if r.get("feedUrl")]
    if not results:
        return None, "no result with feedUrl", []
    ranked = sorted(results, key=lambda r: score(r, title, author), reverse=True)
    best = ranked[0]
    alts = [
        {"name": r.get("collectionName"), "author": r.get("artistName"),
         "feed": r.get("feedUrl"), "score": round(score(r, title, author), 1)}
        for r in ranked[1:4]
    ]
    return {
        "itunes_id": best.get("collectionId"),
        "itunes_name": best.get("collectionName"),
        "itunes_author": best.get("artistName"),
        "feed": best.get("feedUrl"),
        "artwork": best.get("artworkUrl600"),
        "genres": best.get("genres"),
        "episode_count": best.get("trackCount"),
        "last_release": best.get("releaseDate"),
        "match_score": round(score(best, title, author), 1),
    }, None, alts


def main():
    seed_path = sys.argv[1] if len(sys.argv) > 1 else "data/shows_seed.txt"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "data/feeds.json"

    shows = []
    with open(seed_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            while len(parts) < 3:
                parts.append("")
            shows.append({"display": parts[0], "author": parts[1], "query": parts[2]})

    out = []
    for i, sh in enumerate(shows, 1):
        res, err, alts = resolve(sh["display"], sh["author"], sh["query"])
        rec = {"display": sh["display"], "author": sh["author"]}
        if res:
            rec.update(res)
            rec["alternatives"] = alts
            flag = "OK " if res["match_score"] >= 80 else "CHECK"
            print(f"[{i:2}/{len(shows)}] {flag} {sh['display'][:34]:34} -> "
                  f"{res['itunes_name'][:40]:40} ({res['match_score']})", flush=True)
        else:
            rec["error"] = err
            print(f"[{i:2}/{len(shows)}] FAIL {sh['display'][:34]:34} -> {err}", flush=True)
        out.append(rec)
        time.sleep(0.35)  # 对 iTunes 客气一点，避免 403

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in out if r.get("feed"))
    check = sum(1 for r in out if r.get("feed") and r.get("match_score", 0) < 80)
    print(f"\n解析到 feed: {ok}/{len(out)}；其中需人工确认(<80分): {check}")
    print(f"写入 {out_path}")


if __name__ == "__main__":
    main()
