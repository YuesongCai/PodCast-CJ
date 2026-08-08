#!/usr/bin/env python3
"""把每集正文压成有界的「证据包」，组装成一份 briefing pack 交给 Claude 判断。

为什么要压：一集 Cognitive Revolution 有 27800 词，10 集就是 20 万词，
塞不进一个 context，也没必要——判断「值不值得听」不需要通读全文。

压缩是纯抽取式的（不用 LLM，零成本）：
  - 开头 N 词：主播通常在这里把本集主旨和嘉宾说清楚
  - 结尾 N 词：结论和 takeaway
  - 中间按信息密度选句：带数字/金额/百分比、专有名词、观点动词
    （think/argue/expect/because…）的句子优先，保持原文顺序

这样每集 ~1800 词，10 集 ~2.4 万 token，一个 context 轻松装下，
LLM 的活儿就纯粹是「筛选 + 判断」——正是这个需求的价值所在。

用法：
    python3 pipeline/build_digest.py
    python3 pipeline/build_digest.py --budget-words 2500
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CACHE, DATA, load_json, now_utc, save_json

TX_DIR = os.path.join(CACHE, "transcripts")

HEAD_WORDS = 450     # 开头保留
TAIL_WORDS = 250     # 结尾保留
BUDGET_WORDS = 1800  # 每集总预算

NUM_RE = re.compile(r"(\$\s?\d|\d+\s?%|\b\d{2,}\b|\bbillion\b|\bmillion\b|\btrillion\b|\bbps\b)", re.I)
CLAIM_RE = re.compile(
    r"\b(i think|i believe|we think|my view|the point is|the reason|because|"
    r"argue|expect|forecast|predict|the thesis|turns out|the key|"
    r"mistake|lesson|surprising|counterintuitive|disagree|wrong about|"
    r"what matters|bottom line|the problem is|the risk|opportunity)\b", re.I)
ENTITY_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})?\b")
FILLER_RE = re.compile(
    r"^\s*(yeah|yes|no|right|okay|ok|sure|exactly|totally|mm+|uh+|um+|"
    r"thank you|thanks|welcome back|absolutely|got it|i see|wow|hmm)[\s.,!?]*$", re.I)


# 口播广告特征。Masters of Scale 开头 1400 词全是广告（Creative Planning / Deel /
# 峰会推广），而「开头 450 词」这个预算本来是留给主播交代本集主旨的——
# 不切掉广告，最值钱的那段预算就被浪费了。
AD_RE = re.compile(
    r"(brought to you by|sponsored by|our sponsor|this episode is sponsored|"
    r"promo code|discount code|use code |visit [a-z0-9.\-]+\.com|"
    r"learn more at|sign up at|get started at|try it (free|today)|"
    r"go to [a-z0-9.\-]+\.com|the platform used by|"
    r"\.com\s*/?\s*slash|dot com slash|that's [a-z]( [a-z]){2,} dot com|"
    r"free trial|terms and conditions apply|start your free)", re.I)

# 主播口播的节目自我推广（订阅/评分/加入会员），同样是噪音
PROMO_RE = re.compile(
    r"(subscribe (to|on) (apple|spotify|youtube)|leave (us )?a (review|rating)|"
    r"five[- ]star review|join us at|rate and review|"
    r"follow (us|the show) on|link in the (show notes|description))", re.I)


AD_LOOKBACK = 8      # 锚点往前回溯几句（广告开头往往不含 URL，读起来像正文）
AD_LOOKAHEAD = 2
PREROLL_ZONE = 0.25  # 前 25% 里出现锚点，视为片头广告块
MAX_AD_FRACTION = 0.30   # 安全阀：最多切掉三成，防误杀正文


def strip_ads(text):
    """删掉口播广告块。返回 (清洗后文本, 删掉的词数)。

    关键：广告是**连续块**，不是孤立句子。Masters of Scale 片头 1400 词广告里，
    只有「Learn more at creativeplanning.com」这一句带特征词，
    前面「The very best founders I know are brilliant at building systems...」
    完全读得像正文。所以命中锚点后必须向前回溯整块，而不是只删那一句。
    """
    if not text:
        return text, 0
    sents = sentences(text)
    n = len(sents)
    if n < 8:
        return text, 0

    anchors = [i for i, s in enumerate(sents) if AD_RE.search(s) or PROMO_RE.search(s)]
    if not anchors:
        return text, 0

    is_ad = [False] * n
    for i in anchors:
        for k in range(max(0, i - AD_LOOKBACK), min(n, i + AD_LOOKAHEAD + 1)):
            is_ad[k] = True

    # 片头广告块：前 25% 内最后一个锚点之前的内容整段视为广告
    zone_end = int(n * PREROLL_ZONE)
    early = [i for i in anchors if i <= zone_end]
    if early:
        for k in range(0, min(n, max(early) + AD_LOOKAHEAD + 1)):
            is_ad[k] = True

    ad_words = sum(len(sents[i].split()) for i in range(n) if is_ad[i])
    total_words = sum(len(s.split()) for s in sents)
    if ad_words > total_words * MAX_AD_FRACTION:
        # 判定过于激进，退回只删锚点句本身
        kept, dropped = [], 0
        for i, s in enumerate(sents):
            if i in anchors:
                dropped += len(s.split())
            else:
                kept.append(s)
        return " ".join(kept), dropped

    return " ".join(sents[i] for i in range(n) if not is_ad[i]), ad_words


def sentences(text):
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def score_sentence(s):
    w = len(s.split())
    if w < 6 or w > 70 or FILLER_RE.match(s):
        return -1
    sc = 0.0
    sc += 2.0 * len(NUM_RE.findall(s))
    sc += 3.0 if CLAIM_RE.search(s) else 0.0
    sc += 0.6 * min(len(set(ENTITY_RE.findall(s))), 5)
    sc += 1.0 if 12 <= w <= 45 else 0.0     # 偏好信息完整的中长句
    return sc


def compress(text, budget=BUDGET_WORDS, head=HEAD_WORDS, tail=TAIL_WORDS):
    """抽取式压缩。返回 (压缩文本, 统计)。"""
    raw_total = len((text or "").split())
    text, ad_words = strip_ads(text)          # 先切广告，否则开头预算被广告吃掉
    words_all = text.split()
    total = len(words_all)
    if total <= budget:
        return text.strip(), {"original_words": raw_total, "kept_words": total,
                              "ad_words_removed": ad_words,
                              "ratio": round(total / max(raw_total, 1), 3),
                              "method": "去广告后未超预算，全量保留"}

    head_txt = " ".join(words_all[:head])
    tail_txt = " ".join(words_all[-tail:])
    middle = " ".join(words_all[head:-tail])

    mid_budget = max(budget - head - tail, 200)
    sents = sentences(middle)
    scored = [(score_sentence(s), i, s) for i, s in enumerate(sents)]
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: x[0], reverse=True)

    picked, used = [], 0
    for sc, i, s in scored:
        n = len(s.split())
        if used + n > mid_budget:
            continue
        picked.append((i, s))
        used += n
        if used >= mid_budget * 0.97:
            break
    picked.sort()                                    # 恢复原文顺序，保住逻辑流
    mid_txt = " […] ".join(s for _, s in picked)

    out = (f"【开头原文】\n{head_txt}\n\n"
           f"【中段·按信息密度抽取（非连续，[…] 表示跳过）】\n{mid_txt}\n\n"
           f"【结尾原文】\n{tail_txt}")
    kept = len(out.split())
    return out, {"original_words": raw_total, "kept_words": kept,
                 "ad_words_removed": ad_words,
                 "ratio": round(kept / max(raw_total, 1), 3),
                 "middle_sentences_kept": len(picked),
                 "method": "去广告 + 开头/结尾原文 + 中段信息密度抽取"}


def load_text(ep_id):
    rec = load_json(os.path.join(TX_DIR, ep_id + ".json"), None)
    return rec


def build(episodes, section, budget):
    items = []
    for ep in episodes:
        rec = load_text(ep["id"])
        if rec and rec.get("text"):
            text, source, fidelity = rec["text"], rec["source"], rec.get("fidelity", "?")
            attempts = rec.get("attempts", [])
        else:
            text, source, fidelity = ep.get("notes") or "", "notes_only", "notes_only"
            attempts = []

        body, stats = compress(text, budget)
        yt = None
        for a in attempts:
            if a.get("source") == "youtube" and a.get("hit"):
                yt = (a.get("meta") or {}).get("url")
        items.append({
            "section": section,
            "episode_id": ep["id"],
            "show": ep["show"],
            "title": ep["title"],
            "published": ep.get("published_date"),
            "duration_min": round(ep["duration_sec"] / 60) if ep.get("duration_sec") else None,
            "link": ep.get("link"),
            "youtube": yt,
            "evidence_source": source,
            "fidelity": fidelity,
            "notes": (ep.get("notes") or "")[:1200],
            "compression": stats,
            "evidence": body,
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subscribed", default=os.path.join(DATA, "episodes.json"))
    ap.add_argument("--watchlist", default=os.path.join(DATA, "watchlist_episodes.json"))
    ap.add_argument("--out", default=os.path.join(DATA, "digest_pack.json"))
    ap.add_argument("--budget-words", type=int, default=BUDGET_WORDS)
    args = ap.parse_args()

    sub = load_json(args.subscribed, {}) or {}
    wat = load_json(args.watchlist, {}) or {}

    items = build(sub.get("episodes", []), "subscribed", args.budget_words)
    items += build(wat.get("episodes", []), "watchlist", args.budget_words)

    from collections import Counter
    fid = Counter(i["fidelity"] for i in items)
    src = Counter(i["evidence_source"] for i in items)
    tot_orig = sum(i["compression"]["original_words"] for i in items)
    tot_kept = sum(i["compression"]["kept_words"] for i in items)

    pack = {
        "generated_at": now_utc().isoformat(),
        "counts": {"total": len(items),
                   "subscribed": sum(1 for i in items if i["section"] == "subscribed"),
                   "watchlist": sum(1 for i in items if i["section"] == "watchlist")},
        "fidelity_breakdown": dict(fid),
        "source_breakdown": dict(src),
        "compression": {"original_words": tot_orig, "kept_words": tot_kept,
                        "ratio": round(tot_kept / max(tot_orig, 1), 3),
                        "est_tokens": round(tot_kept * 1.35)},
        "items": items,
    }
    save_json(args.out, pack)

    print(f"{'节目':24} {'区':10} {'可信度':11} {'原词数':>7} {'压后':>6}  标题")
    print("-" * 108)
    for i in items:
        c = i["compression"]
        print(f"{i['show'][:23]:24} {i['section']:10} {i['fidelity']:11} "
              f"{c['original_words']:>7} {c['kept_words']:>6}  {i['title'][:36]}")
    print(f"\n条目 {len(items)} 条（订阅 {pack['counts']['subscribed']} / "
          f"盲区 {pack['counts']['watchlist']}）")
    print("可信度分布：", dict(fid))
    print("来源分布：", dict(src))
    print(f"压缩：{tot_orig} 词 -> {tot_kept} 词（{pack['compression']['ratio']*100:.0f}%），"
          f"约 {pack['compression']['est_tokens']} tokens")
    print(f"写入 {args.out}")


if __name__ == "__main__":
    main()
