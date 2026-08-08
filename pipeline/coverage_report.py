#!/usr/bin/env python3
"""逐档节目的 transcript 可得性报告——用于决定「哪些必须买/自转，哪些免费就够」。

判据基于已实测的证据，不猜：
  - RSS 里带 <podcast:transcript>   -> 官方全文，免费
  - RSS 描述超长（Substack 类）      -> 免费全文
  - YouTube 频道已锁定且时长校验通过 -> 自动字幕，免费
  - 以上都没有                       -> 缺口，需付费源或本地转录

用法：
    python3 pipeline/coverage_report.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, load_json, now_utc, save_json

INLINE_MIN = 8000


def main():
    feeds = load_json(os.path.join(DATA, "feeds.json"), []) or []
    health = {r["display"]: r for r in load_json(os.path.join(DATA, "feed_health.json"), []) or []}
    yt = load_json(os.path.join(DATA, "youtube_channels.json"), {}) or {}
    # 实测结果（有就用实测覆盖预测）
    tidx = load_json(os.path.join(DATA, "transcripts_index.json"), {}) or {}
    measured = {}
    for it in tidx.get("items", []):
        measured.setdefault(it["show"], []).append(it["source"])

    rows = []
    for f in feeds:
        name = f["display"]
        h = health.get(name, {})
        y = yt.get(name, {})
        eps30 = h.get("eps_last_30d")

        paths, gap_reason = [], None
        if f.get("blocked"):
            gap_reason = {"spotify_only": "Spotify 独占，无公开 RSS",
                          "paywalled_private_rss": "订阅者私有 RSS"}.get(f["blocked"], f["blocked"])
        else:
            if (h.get("transcript_tag_ratio") or 0) > 0:
                paths.append(f"官方transcript({int(h['transcript_tag_ratio']*100)}%集)")
            if (h.get("notes_chars_median") or 0) >= INLINE_MIN:
                paths.append("RSS内嵌全文")
            if y.get("channel"):
                paths.append("YT字幕" + ("✓校验" if y.get("verified") else "(弱)"))
            if not paths:
                gap_reason = "无官方transcript、无内嵌全文、YouTube 上找不到对应频道"

        m = measured.get(name, [])
        rows.append({
            "show": name,
            "eps_30d": eps30,
            "free_paths": paths,
            "gap": gap_reason,
            "measured_sources": m,
            "status": "缺口" if (gap_reason or (m and all(s == "notes_only" for s in m)))
                      else ("已实测OK" if any(s != "notes_only" for s in m) else "预期可行"),
        })

    order = {"缺口": 0, "预期可行": 1, "已实测OK": 2}
    rows.sort(key=lambda r: (order[r["status"]], -(r["eps_30d"] or 0)))

    print(f"{'节目':34} {'30天集数':>7} {'状态':10} 免费路径 / 缺口原因")
    print("-" * 112)
    for r in rows:
        detail = " + ".join(r["free_paths"]) if r["free_paths"] else (r["gap"] or "-")
        if r["measured_sources"]:
            detail += f"   [实测: {','.join(sorted(set(r['measured_sources'])))}]"
        print(f"{r['show'][:33]:34} {str(r['eps_30d'] or '-'):>7} {r['status']:10} {detail[:58]}")

    gaps = [r for r in rows if r["status"] == "缺口"]
    ok = [r for r in rows if r["status"] != "缺口"]
    gap_eps = sum(r["eps_30d"] or 0 for r in gaps)
    all_eps = sum(r["eps_30d"] or 0 for r in rows)
    print(f"\n免费路径覆盖 {len(ok)}/{len(rows)} 档节目")
    print(f"缺口 {len(gaps)} 档：{', '.join(r['show'] for r in gaps)}")
    print(f"按发布量算：缺口档占 30 天 {gap_eps}/{all_eps} 集"
          f"（{gap_eps/max(all_eps,1)*100:.0f}%）")

    save_json(os.path.join(DATA, "coverage_report.json"),
              {"generated_at": now_utc().isoformat(), "rows": rows,
               "summary": {"shows_total": len(rows), "shows_free_ok": len(ok),
                           "shows_gap": len(gaps), "gap_eps_30d": gap_eps,
                           "all_eps_30d": all_eps}})
    print("写入 data/coverage_report.json")


if __name__ == "__main__":
    main()
