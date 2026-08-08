#!/usr/bin/env python3
"""体检每个 feed：能不能拉、更新频率、有没有官方 transcript、show notes 够不够厚。

这一步决定整条流水线的成本：
  - 有 <podcast:transcript> 标签  -> 全文免费直取，最理想
  - show notes 够厚               -> 不用转录也能判断「值不值得听」
  - 两者都没有                     -> 只能靠本地 Whisper 转录（费算力）

用法：
    python3 pipeline/scan_feeds.py data/feeds.json data/feed_health.json
"""
import json
import re
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 podcast-newsletter/0.1"
NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"[ \t]+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s.strip(), fmt)
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def text_of(item, path):
    el = item.find(path, NS)
    return (el.text or "") if el is not None and el.text else ""


def scan(rec):
    out = {"display": rec["display"], "feed": rec.get("feed")}
    if not rec.get("feed"):
        out["status"] = "no_feed"
        return out
    try:
        raw = fetch(rec["feed"])
    except Exception as e:
        out["status"] = "fetch_error"
        out["error"] = str(e)[:200]
        return out
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        out["status"] = "parse_error"
        out["error"] = str(e)[:200]
        return out

    chan = root.find("channel")
    if chan is None:
        out["status"] = "not_rss"
        return out

    items = chan.findall("item")
    out["status"] = "ok"
    out["title"] = text_of(chan, "title")
    out["total_items"] = len(items)

    now = datetime.now(timezone.utc)
    dated = []
    with_tx, notes_len, sample = 0, [], []
    for it in items:
        d = parse_date(text_of(it, "pubDate"))
        if d:
            dated.append(d)
        tx = it.findall("podcast:transcript", NS)
        if tx:
            with_tx += 1
        desc = strip_html(text_of(it, "description") or
                          text_of(it, "content:encoded") or
                          text_of(it, "itunes:summary"))
        notes_len.append(len(desc))
        if len(sample) < 3:
            sample.append({
                "title": text_of(it, "title")[:120],
                "pub": d.strftime("%Y-%m-%d") if d else None,
                "duration": text_of(it, "itunes:duration"),
                "notes_chars": len(desc),
                "notes_head": desc[:260],
                "transcript_urls": [t.attrib.get("url") for t in tx],
                "link": text_of(it, "link")[:200],
            })

    if dated:
        dated.sort(reverse=True)
        out["latest"] = dated[0].strftime("%Y-%m-%d")
        out["days_since_latest"] = (now - dated[0]).days
        out["eps_last_7d"] = sum(1 for d in dated if now - d <= timedelta(days=7))
        out["eps_last_30d"] = sum(1 for d in dated if now - d <= timedelta(days=30))
        if len(dated) >= 6:
            span = (dated[0] - dated[min(len(dated) - 1, 19)]).days or 1
            n = min(len(dated), 20)
            out["avg_days_between"] = round(span / max(n - 1, 1), 1)
    out["items_with_transcript_tag"] = with_tx
    out["transcript_tag_ratio"] = round(with_tx / len(items), 2) if items else 0
    if notes_len:
        notes_len.sort()
        out["notes_chars_median"] = notes_len[len(notes_len) // 2]
    out["samples"] = sample
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "data/feeds.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/feed_health.json"
    feeds = json.load(open(src, encoding="utf-8"))

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(scan, feeds))

    hdr = f"{'节目':36} {'状态':12} {'最新':11} {'7d':>3} {'30d':>4} {'均隔':>5} {'官方TX':>7} {'notes':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(results, key=lambda x: x.get("days_since_latest", 99999)):
        tx = r.get("transcript_tag_ratio", 0)
        print(f"{r['display'][:35]:36} {r.get('status','?'):12} "
              f"{str(r.get('latest','-')):11} {r.get('eps_last_7d','-'):>3} "
              f"{r.get('eps_last_30d','-'):>4} {str(r.get('avg_days_between','-')):>5} "
              f"{(str(int(tx*100))+'%' if tx else '无'):>7} "
              f"{r.get('notes_chars_median','-'):>6}")

    json.dump(results, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ok = [r for r in results if r.get("status") == "ok"]
    print(f"\nfeed 可用 {len(ok)}/{len(results)}")
    print(f"带官方 transcript 标签的节目: {sum(1 for r in ok if r.get('transcript_tag_ratio',0)>0)}")
    print(f"写入 {dst}")


if __name__ == "__main__":
    main()
