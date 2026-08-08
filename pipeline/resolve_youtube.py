#!/usr/bin/env python3
"""给每档节目找到它的 YouTube 频道，缓存下来。

为什么必须做这一步：全局 ytsearch 会被二次搬运、切片、同名视频污染。
实测 YC 的「Garry Tan: Own Your Intelligence」全局搜前 5 条里根本没有正片，
而在 YC 频道内搜第一条就是它、时长差 0.3%。频道锁定同时提升召回和精度。

做法：拿该节目最近几集标题分别去全局搜，统计命中最多的频道，
再要求频道名与节目名/作者名有相似度，最后用一集做时长校验。

用法：
    python3 pipeline/resolve_youtube.py                 # 补全所有缺失的
    python3 pipeline/resolve_youtube.py --show "Grit"   # 只做一档
    python3 pipeline/resolve_youtube.py --refresh       # 全部重做
"""
import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, NS, http_get, load_json, now_utc, parse_duration, save_json
from get_transcript import YT_BIN, title_sim, norm_title, search_query

MAP_PATH = os.path.join(DATA, "youtube_channels.json")


def yt_json(args, timeout=150, retries=2):
    """YouTube 搜索会瞬时限流返回空结果（实测 Chat With Traders / UBS 都中过），
    空结果必须重试，否则会把明明有频道的节目误判成「YouTube 上没有」。"""
    import time as _t
    last_err = ""
    for attempt in range(retries + 1):
        try:
            p = subprocess.run([YT_BIN] + args, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            last_err = str(e)[:100]
            _t.sleep(2 + attempt * 3)
            continue
        rows = []
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        if rows:
            return rows, ""
        last_err = p.stderr.strip()[:120] or "empty result"
        if attempt < retries:
            _t.sleep(3 + attempt * 4)
    return [], last_err


def recent_episodes(feed_url, n=4):
    """从 RSS 拿最近 n 集的 (标题, 时长秒)。"""
    try:
        root = ET.fromstring(http_get(feed_url))
    except Exception:
        return []
    chan = root.find("channel")
    if chan is None:
        return []
    out = []
    for it in chan.findall("item")[: n * 3]:
        te = it.find("title")
        title = (te.text or "").strip() if te is not None and te.text else ""
        de = it.find("itunes:duration", NS)
        dur = parse_duration((de.text or "") if de is not None and de.text else "")
        if title and dur and dur > 300:      # 跳过超短集，时长校验更可靠
            out.append((title, dur))
        if len(out) >= n:
            break
    return out


def channel_scoped_search(channel_url, query, limit=5):
    base = channel_url.rstrip("/")
    from urllib.parse import quote
    url = f"{base}/search?query={quote(query)}"
    rows, _ = yt_json(["--no-warnings", "--skip-download", "--dump-json",
                       "--flat-playlist", "--playlist-items", f"1-{limit}",
                       "--socket-timeout", "25", url], timeout=180)
    return rows


def guess_channel(show, feed_url):
    """返回 (channel_info, diagnosis)。"""
    eps = recent_episodes(feed_url, 4)
    if not eps:
        return None, "RSS 里取不到可用集"

    tally = Counter()
    meta = {}
    for title, dur in eps:
        rows, _ = yt_json(["--no-warnings", "--skip-download", "--dump-json",
                           "--flat-playlist", "--playlist-items", "1-6",
                           "--socket-timeout", "20", f"ytsearch6:{search_query(title, show['display'])}"],
                          timeout=150)
        for r in rows:
            ch = r.get("channel") or r.get("uploader")
            cid = r.get("channel_id") or r.get("uploader_id")
            if not ch:
                continue
            sim = title_sim(title, r.get("title") or "")
            ydur = r.get("duration")
            dur_close = bool(ydur and abs(ydur - dur) / dur <= 0.10)
            # 标题像 + 时长近 = 强证据；只是同频道出现 = 弱证据
            weight = 3 if (sim >= 0.6 and dur_close) else (1 if sim >= 0.4 else 0.2)
            tally[ch] += weight
            meta.setdefault(ch, {"channel_id": cid,
                                 "channel_url": r.get("channel_url") or
                                 (f"https://www.youtube.com/channel/{cid}" if cid else None)})

    if not tally:
        return None, "全局搜索无任何结果"

    # 频道名要和节目名/作者名/Apple 上的正式名对得上，避免锁到搬运号。
    # 必须带上 itunes_name：CJ 列表里显示的是「David Senra」，节目正式名却是
    # 「Founders」，只比 display 会把正确频道当证据不足丢掉。
    aliases = [a for a in (show.get("display"), show.get("author"),
                           show.get("itunes_name"), show.get("itunes_author"),
                           f"{show.get('display')} {show.get('author') or ''}") if a]
    ranked = []
    for ch, score in tally.most_common(8):
        nsim = max(title_sim(a, ch) for a in aliases)
        # 别名被频道名包含（或反之）也算强证据
        for a in aliases:
            na, nc = norm_title(a), norm_title(ch)
            if na and nc and (na in nc or nc in na):
                nsim = max(nsim, 0.9)
        ranked.append((score + nsim * 4, score, nsim, ch))
    ranked.sort(reverse=True)
    total, score, nsim, ch = ranked[0]
    info = {"channel": ch, **meta[ch], "evidence_score": round(score, 1),
            "name_sim": round(nsim, 2)}

    if nsim < 0.35 and score < 6:
        return None, (f"最佳候选 '{ch}' 证据不足（name_sim {nsim:.2f}, score {score:.1f}）"
                      f"；候选: {[r[3] for r in ranked[:3]]}")

    # 用一集做频道内校验
    if info.get("channel_url"):
        title, dur = eps[0]
        rows = channel_scoped_search(info["channel_url"], search_query(title), 5)
        best = None
        for r in rows:
            sim = title_sim(title, r.get("title") or "")
            ydur = r.get("duration")
            if ydur and sim >= 0.5:
                diff = abs(ydur - dur) / dur
                if best is None or diff < best[0]:
                    best = (diff, sim, r.get("title"))
        if best:
            info["verify"] = {"episode": title[:60], "dur_diff": f"{best[0]*100:.1f}%",
                              "title_sim": round(best[1], 2), "matched": best[2][:60]}
            info["verified"] = best[0] <= 0.10 and best[1] >= 0.5
        else:
            info["verify"] = {"episode": title[:60], "note": "频道内搜不到该集"}
            info["verified"] = False
    return info, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default=os.path.join(DATA, "feeds.json"))
    ap.add_argument("--show", default=None, help="只处理这一档")
    ap.add_argument("--refresh", action="store_true", help="忽略已有缓存全部重做")
    args = ap.parse_args()

    shows = load_json(args.feeds, [])
    cur = load_json(MAP_PATH, {}) or {}

    todo = [s for s in shows if s.get("feed")]
    if args.show:
        todo = [s for s in todo if s["display"].lower() == args.show.lower()]
    if not args.refresh:
        todo = [s for s in todo if s["display"] not in cur]

    print(f"待解析 {len(todo)} 档（已缓存 {len(cur)} 档）\n")
    for i, s in enumerate(todo, 1):
        info, err = guess_channel(s, s["feed"])
        if info:
            v = info.get("verified")
            mark = "OK  " if v else "弱  "
            cur[s["display"]] = info
            vd = info.get("verify", {})
            print(f"[{i:2}/{len(todo)}] {mark} {s['display'][:30]:31} -> {info['channel'][:32]:33} "
                  f"name_sim={info['name_sim']} 校验={vd.get('dur_diff', vd.get('note','-'))}")
        else:
            cur[s["display"]] = {"channel": None, "error": err}
            print(f"[{i:2}/{len(todo)}] FAIL {s['display'][:30]:31} -> {err[:70]}")
        save_json(MAP_PATH, cur)

    ok = sum(1 for v in cur.values() if v.get("channel"))
    ver = sum(1 for v in cur.values() if v.get("verified"))
    print(f"\n锁定频道 {ok}/{len(cur)}，其中通过时长校验 {ver}")
    print(f"写入 {MAP_PATH}")


if __name__ == "__main__":
    main()
