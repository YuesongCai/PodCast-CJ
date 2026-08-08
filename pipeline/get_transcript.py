#!/usr/bin/env python3
"""按「免费优先」的来源链给每集拿正文。

来源链（从便宜到贵，命中即停）：
  1 rss_inline      RSS 描述里就带全文（Substack/Ghost 类，如 Latent Space）
  2 rss_transcript  RSS 里有 <podcast:transcript>（vtt/srt/json/html/pdf）
  3 youtube         YouTube 自动字幕（免费、免登录；靠时长+标题双重校验防错配）
  4 website         节目页自带 transcript
  5 asr             本地转录 RSS 音频（默认开启，覆盖率兜底）
  6 notes_only      降级：只有 show notes，够做「值不值得听」但给不出细节

两个关键设计：

1) 第 3 步的错配防御。YouTube 搜到的视频必须同时满足标题相似度阈值 +
   时长与 RSS 相差 <8%，否则拒绝——宁可降级也不要张冠李戴。实测这条拦住了
   「RSS 是 11 分钟 highlights、YouTube 是 60 分钟正片」这类假匹配。

2) 第 5 步是覆盖率的保底。RSS 一定带 audio enclosure，所以只要节目有公开
   RSS，本地转录就一定能出全文。前 4 步都是「有就白拿」的加速层。

用法：
    python3 pipeline/get_transcript.py --episodes data/episodes.json
    python3 pipeline/get_transcript.py --probe          # 只探测来源，不下正文
    python3 pipeline/get_transcript.py --no-asr         # 只看免费快路径的覆盖率
"""
import argparse
import difflib
import html
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CACHE, DATA, http_get, load_json, now_utc, save_json, strip_html

TX_DIR = os.path.join(CACHE, "transcripts")
YT_BIN = os.path.expanduser("~/.local/bin/yt-dlp")

INLINE_FULLTEXT_MIN = 8000   # notes 超过这个长度，基本可以当全文用
WEBSITE_GAIN_MIN = 3.0       # 网页正文要比 notes 长这么多倍才考虑
DUR_TOLERANCE = 0.08         # YouTube 时长与 RSS 相差上限
TITLE_SIM_MIN = 0.55         # 标题相似度下限

# 播客托管/播放器域名：这些页面只有播放器和导航，永远没有 transcript。
# 之前用「长度增益」判据在这些站上全军覆没——抓到的是整档节目的其它集列表。
PLAYER_DOMAINS = (
    "podcasters.spotify.com", "open.spotify.com", "anchor.fm", "acast.com",
    "podbean.com", "libsyn.com", "megaphone.fm", "buzzsprout.com",
    "simplecast.com", "transistor.fm", "captivate.fm", "redcircle.com",
    "podtrac.com", "blubrry.com", "spreaker.com", "castos.com",
    "podcast.show", "fireside.fm", "art19.com", "omnystudio.com",
    "audioboom.com", "soundcloud.com", "apple.com", "iheart.com",
)

# transcript 特征词：真人对话里高频，样板页面里几乎不出现
CONVO_MARKERS = ("i ", "you ", "we ", "think", "really", "yeah", "right",
                 "know", "like", "that's", "it's", "so ", "but ", "kind of")


# ---------- 文本归一化 ----------

def norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"^\s*(ep(isode)?\.?\s*)?#?\d{1,4}\s*[:.\-–|·]\s*", "", s)  # 去开头集号
    s = re.sub(r"\b(highlights?|full episode|part \d|audio only)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def search_query(title, show=""):
    """把 RSS 标题清成适合喂 YouTube 搜索的形式。

    RSS 标题常带集号前缀和分隔符，实测「329 · Peter Brandt - How a 50-Year
    Veteran Thinks About Risk Management」整串去搜返回 0 结果，
    去掉「329 ·」后第一条就是正片。这个清洗直接决定 YouTube 源的召回。
    """
    s = title or ""
    s = re.sub(r"^\s*(ep(isode)?\.?\s*)?#?\d{1,4}\s*[:.\-–—|·•]\s*", "", s, flags=re.I)
    s = re.sub(r"^\s*#\d{1,5}\s*[-–—:]?\s*", "", s)          # 「#2537 - David Sinclair」
    s = re.sub(r"[·•|]+", " ", s)                              # 中点/竖线会干扰检索
    s = re.sub(r"\s*[\(\[](audio|video|full episode|part \d+)[\)\]]\s*", " ", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" -–—:")
    if len(s) < 8:                                             # 清得太狠就退回原标题
        s = title or ""
    return f"{s} {show}".strip() if show else s


def title_sim(a, b):
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    base = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))   # 覆盖率，抗长度差
    return max(base, overlap)


def clean_transcript(text):
    """压掉说话人标签行外的噪音、重复行、时间戳残留。"""
    if not text:
        return ""
    text = re.sub(r"\[(music|applause|laughter|inaudible|silence)[^\]]*\]", " ", text, flags=re.I)
    lines, out, prev = text.splitlines(), [], None
    for ln in lines:
        ln = ln.strip()
        if not ln or ln == prev:
            continue
        out.append(ln)
        prev = ln
    s = "\n".join(out)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def words(s):
    return len((s or "").split())


# ---------- 各来源实现 ----------

def vtt_to_text(raw):
    """VTT/SRT -> 纯文本。YouTube 自动字幕是滚动重复的，要去重。"""
    txt = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    out, seen_tail = [], ""
    for ln in txt.splitlines():
        ln = ln.strip()
        if (not ln or ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE"))
                or "-->" in ln or ln.isdigit()):
            continue
        ln = re.sub(r"<[^>]+>", "", ln)          # 去 <c>/<00:00:00.000> 内联标签
        ln = html.unescape(ln).strip()
        if not ln:
            continue
        # 滚动字幕去重：新行常包含上一行的尾部
        if seen_tail and ln in seen_tail:
            continue
        out.append(ln)
        seen_tail = ln
    # 二次去重相邻重复
    dedup, prev = [], None
    for x in out:
        if x != prev:
            dedup.append(x)
        prev = x
    return " ".join(dedup)


def json3_to_text(raw):
    """json3 字幕 -> 纯文本。

    注意：字幕换行在 json3 里是独立的 "\\n" seg。早先直接跳过它，
    结果把「can save」粘成「cansave」、「do tasks」粘成「dotasks」——
    换行必须替换成空格，不能丢。
    """
    d = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    parts = []
    for ev in d.get("events", []):
        for seg in ev.get("segs", []) or []:
            u = seg.get("utf8")
            if not u:
                continue
            parts.append(" " if u == "\n" else u)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def from_rss_inline(ep):
    if ep.get("notes_chars", 0) >= INLINE_FULLTEXT_MIN:
        return clean_transcript(ep["notes"]), {"note": f"RSS 描述内含长正文 {ep['notes_chars']} 字符"}
    return None, None


def from_rss_transcript(ep):
    for tx in ep.get("rss_transcripts") or []:
        url, typ = tx.get("url"), (tx.get("type") or "").lower()
        if not url:
            continue
        try:
            raw = http_get(url, timeout=60)
        except Exception as e:
            return None, {"error": f"{url[:70]}: {str(e)[:60]}"}
        try:
            if "json" in typ or url.endswith(".json"):
                d = json.loads(raw.decode("utf-8", "replace"))
                segs = d.get("segments") or d.get("results") or []
                text = " ".join(s.get("body") or s.get("text") or "" for s in segs)
            elif "vtt" in typ or "srt" in typ or url.endswith((".vtt", ".srt")):
                text = vtt_to_text(raw)
            elif "pdf" in typ or url.endswith(".pdf"):
                text = pdf_to_text(raw)
            else:
                text = strip_html(raw.decode("utf-8", "replace"))
            text = clean_transcript(text)
            if words(text) > 300:
                return text, {"url": url, "type": typ or "auto"}
        except Exception as e:
            return None, {"error": f"parse {url[:60]}: {str(e)[:60]}"}
    return None, None


def pdf_to_text(raw):
    """不依赖第三方库的极简 PDF 文本提取（够用于 transcript PDF）。"""
    try:
        import zlib
    except ImportError:
        return ""
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        blob = m.group(1)
        try:
            blob = zlib.decompress(blob)
        except Exception:
            pass
        for tm in re.finditer(rb"\((?:[^()\\]|\\.)*\)", blob):
            s = tm.group(0)[1:-1]
            s = re.sub(rb"\\([()\\])", rb"\1", s)
            try:
                chunks.append(s.decode("utf-8", "replace"))
            except Exception:
                pass
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def yt_run(args, timeout=180):
    try:
        p = subprocess.run([YT_BIN] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "yt-dlp not installed"


_YT_CHANNELS = None


def yt_channel_for(show):
    """读 data/youtube_channels.json 里缓存的频道。"""
    global _YT_CHANNELS
    if _YT_CHANNELS is None:
        _YT_CHANNELS = load_json(os.path.join(DATA, "youtube_channels.json"), {}) or {}
    info = _YT_CHANNELS.get(show) or {}
    return info.get("channel_url") if info.get("channel") else None


def _yt_query(spec, items=6, timeout=150):
    rc, out, err = yt_run(["--no-warnings", "--skip-download", "--dump-json",
                           "--flat-playlist", "--playlist-items", f"1-{items}",
                           "--socket-timeout", "25", spec], timeout=timeout)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows, err


def yt_find(ep):
    """在 YouTube 上找对应视频。返回 (info, reject_reason)。

    先在该节目自己的频道内搜（精度高得多），搜不到再退回全局搜。
    两条路都必须过「标题相似度 + 时长」双重校验。
    """
    from urllib.parse import quote
    cands, scope = [], None

    q = search_query(ep["title"])
    ch_url = yt_channel_for(ep["show"])
    if ch_url:
        rows, _ = _yt_query(f"{ch_url.rstrip('/')}/search?query={quote(q)}",
                            items=6, timeout=180)
        if rows:
            cands, scope = rows, "channel"

    if not cands:
        rows, err = _yt_query(f"ytsearch6:{q} {ep['show']}", items=6, timeout=120)
        if not rows:
            return None, f"no candidates ({err.strip()[:60]})" if err else "no candidates"
        cands, scope = rows, "global"

    rss_dur = ep.get("duration_sec")
    best, best_reason = None, "no candidate passed checks"
    for c in cands:
        vid_title = c.get("title") or ""
        sim = title_sim(ep["title"], vid_title)
        ydur = c.get("duration")
        dur_ok, dur_note = None, "rss 无时长"
        if rss_dur and ydur:
            diff = abs(ydur - rss_dur) / max(rss_dur, 1)
            dur_ok = diff <= DUR_TOLERANCE
            dur_note = f"时长差 {diff*100:.1f}%"
        # 判定：标题够像 且 （时长匹配 或 无时长可比但标题非常像）
        passed = sim >= TITLE_SIM_MIN and (dur_ok is True or (dur_ok is None and sim >= 0.8))
        cand = {"id": c.get("id"), "yt_title": vid_title, "yt_duration": ydur,
                "title_sim": round(sim, 3), "dur_check": dur_note, "scope": scope,
                "channel": c.get("channel") or c.get("uploader"),
                "url": f"https://www.youtube.com/watch?v={c.get('id')}"}
        if passed:
            return cand, None
        if best is None:
            best = cand
            best_reason = f"最佳候选未过校验({scope}: 相似度 {sim:.2f}，{dur_note})"
    return None, best_reason


def yt_captions(video_id):
    """下英文自动字幕。优先人工字幕，其次自动字幕。"""
    with tempfile.TemporaryDirectory() as td:
        rc, out, err = yt_run([
            "--no-warnings", "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en,en-US,en-GB,en-orig,en.*",
            "--sub-format", "json3/vtt/srt",
            "--socket-timeout", "20",
            "-o", os.path.join(td, "cap.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ], timeout=240)
        files = sorted(os.listdir(td))
        if not files:
            return None, f"no subtitle file ({err.strip()[:80]})"
        # 优先 json3
        files.sort(key=lambda f: (0 if f.endswith(".json3") or f.endswith(".json") else 1, f))
        for fn in files:
            path = os.path.join(td, fn)
            raw = open(path, "rb").read()
            try:
                text = json3_to_text(raw) if fn.endswith((".json3", ".json")) else vtt_to_text(raw)
            except Exception:
                continue
            text = clean_transcript(text)
            if words(text) > 200:
                return text, fn
        return None, "subtitle parsed but too short"


def from_youtube(ep):
    cand, reason = yt_find(ep)
    if not cand:
        return None, {"error": reason}
    text, how = yt_captions(cand["id"])
    if not text:
        return None, {**cand, "error": how}
    return text, {**cand, "sub_file": how}


def transcript_likeness(text):
    """判断一段文本「像不像口语 transcript」，而不是网页样板。

    只靠长度会被托管站的「其它集列表」骗过去，所以看结构：
      - 句子平均长度落在口语区间
      - 对话特征词密度
      - 短碎片行（导航/按钮文字）占比
    返回 (score 0~1, 诊断信息)。
    """
    if not text or words(text) < 800:
        return 0.0, {"reason": "太短"}
    low = " " + text.lower() + " "
    wc = words(text)

    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    avg_sent = wc / max(len(sentences), 1)
    sent_ok = 1.0 if 6 <= avg_sent <= 45 else 0.0

    marker_hits = sum(low.count(m) for m in CONVO_MARKERS)
    marker_density = marker_hits / wc                    # 口语正文通常 >0.06
    marker_ok = min(marker_density / 0.06, 1.0)

    lines = [l for l in text.splitlines() if l.strip()]
    frag = sum(1 for l in lines if len(l.split()) < 4) / max(len(lines), 1)
    frag_ok = 1.0 if frag < 0.35 else max(0.0, 1 - (frag - 0.35) * 3)

    score = 0.4 * marker_ok + 0.35 * sent_ok + 0.25 * frag_ok
    return score, {"words": wc, "avg_sentence_words": round(avg_sent, 1),
                   "convo_density": round(marker_density, 4),
                   "fragment_line_ratio": round(frag, 2),
                   "likeness": round(score, 2)}


def from_website(ep):
    link = ep.get("link")
    if not link or not link.startswith("http"):
        return None, None
    host = re.sub(r"^https?://", "", link).split("/")[0].lower()
    if any(host.endswith(d) or d in host for d in PLAYER_DOMAINS):
        return None, {"url": link, "note": f"{host} 是播放器/托管域名，跳过（不会有 transcript）"}
    try:
        raw = http_get(link, timeout=45)
    except Exception as e:
        return None, {"error": f"{str(e)[:70]}"}
    page = strip_html(raw.decode("utf-8", "replace"))
    base = max(ep.get("notes_chars", 0), 500)
    if len(page) < base * WEBSITE_GAIN_MIN or words(page) < 1500:
        return None, {"url": link, "page_chars": len(page), "note": "网页正文不足"}
    score, diag = transcript_likeness(page)
    if score < 0.7:
        return None, {"url": link, "note": "网页正文结构不像 transcript，拒用", **diag}
    return clean_transcript(page), {"url": link, "page_chars": len(page), **diag}


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT_DIR, ".venv", "bin", "python")
ASR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_local.py")


def from_asr(ep, model=None):
    """本地转录 RSS 音频。这是覆盖率的兜底——RSS 都带 audio enclosure，
    所以只要节目在 RSS 上，这条路就一定能出全文。

    实测（M3 / parakeet-tdt-0.6b-v3）：11.4 分钟音频 57 秒出结果，12x 实时。
    跑在 .venv 子进程里，避免主流水线依赖 MLX。
    """
    audio = ep.get("audio_url")
    if not audio:
        return None, {"error": "RSS 里没有 audio enclosure，无法转录"}
    import shutil
    if not shutil.which("ffmpeg"):
        return None, {"error": "ffmpeg 未安装（brew install ffmpeg）"}
    if not os.path.exists(VENV_PY):
        return None, {"error": f"找不到 {VENV_PY}，先建 venv 并装 parakeet-mlx"}

    env = dict(os.environ)
    env.setdefault("HF_HUB_DISABLE_XET", "1")   # xet 传输实测会报 CAS 错误
    env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
    if model:
        env["ASR_MODEL"] = model

    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "asr.json")
        # 超时按时长给：12x 实时 + 下载余量，最少 10 分钟
        dur = ep.get("duration_sec") or 3600
        timeout = max(600, int(dur / 6) + 600)
        try:
            p = subprocess.run([VENV_PY, ASR_SCRIPT, "--audio-url", audio, "--out", out_path],
                               capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return None, {"error": f"转录超时（>{timeout}s）"}
        if not os.path.exists(out_path):
            tail = (p.stdout or p.stderr or "")[-300:]
            return None, {"error": f"ASR 无输出: {tail}"}
        try:
            d = json.load(open(out_path, encoding="utf-8"))
        except Exception as e:
            return None, {"error": f"ASR 输出解析失败: {str(e)[:100]}"}
        if not d.get("ok"):
            return None, {"error": d.get("error", "ASR 失败"), **(d.get("stats") or {})}
        text = clean_transcript(d.get("text") or "")
        if words(text) < 200:
            return None, {"error": f"转录过短 {words(text)} 词", **(d.get("stats") or {})}
        return text, d.get("stats") or {}


# ---------- 主链 ----------

CHAIN = [
    ("rss_inline", from_rss_inline),
    ("rss_transcript", from_rss_transcript),
    ("youtube", from_youtube),
    ("website", from_website),
]

# 正文可信度分级，直接决定 newsletter 里能不能给「细节级 key points」。
# 这个字段必须一路带到最终产出：读者要能分清「这是全文提炼」还是「这只是简介推断」。
FIDELITY = {
    "rss_transcript": "full",     # 官方 transcript，最可信
    "youtube": "full",            # 自动字幕，偶有错词但内容完整
    "asr": "full",                # 本地转录（Parakeet MLX）
    "rss_inline": "full",         # Substack 类全文
    "website": "partial",         # 过了结构校验但仍可能夹杂样板
    "notes_only": "notes_only",   # 只有简介 —— 只能judge值不值得听，不能给细节
}


def fidelity_of(source, text):
    base = FIDELITY.get(source, "notes_only")
    if base == "full" and words(text) < 1500:
        return "partial"          # 全文源但太短，降级
    return base


def resolve_one(ep, allow_asr=True, asr_model=None, probe=False):
    cached = os.path.join(TX_DIR, ep["id"] + ".json")
    if os.path.exists(cached) and not probe:
        c = load_json(cached, {})
        if c.get("words", 0) > 200:
            c["from_cache"] = True
            return c

    attempts = []
    for name, fn in CHAIN:
        try:
            text, meta = fn(ep)
        except Exception as e:
            text, meta = None, {"error": f"exception: {str(e)[:120]}"}
        attempts.append({"source": name, "hit": bool(text), "meta": meta})
        if text:
            rec = {"episode_id": ep["id"], "show": ep["show"], "title": ep["title"],
                   "source": name, "words": words(text), "chars": len(text),
                   "fidelity": fidelity_of(name, text),
                   "attempts": attempts, "resolved_at": now_utc().isoformat(),
                   "text": text}
            if not probe:
                save_json(cached, rec)
            return rec

    if allow_asr:
        text, meta = from_asr(ep, asr_model)
        attempts.append({"source": "asr", "hit": bool(text), "meta": meta})
        if text:
            rec = {"episode_id": ep["id"], "show": ep["show"], "title": ep["title"],
                   "source": "asr", "words": words(text), "chars": len(text),
                   "fidelity": fidelity_of("asr", text),
                   "attempts": attempts, "resolved_at": now_utc().isoformat(), "text": text}
            if not probe:
                save_json(cached, rec)
            return rec

    notes = ep.get("notes") or ""
    return {"episode_id": ep["id"], "show": ep["show"], "title": ep["title"],
            "source": "notes_only", "words": words(notes), "chars": len(notes),
            "fidelity": "notes_only",
            "attempts": attempts, "resolved_at": now_utc().isoformat(), "text": notes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", default=os.path.join(DATA, "episodes.json"))
    ap.add_argument("--out", default=os.path.join(DATA, "transcripts_index.json"))
    ap.add_argument("--probe", action="store_true", help="只探测来源，不写缓存")
    ap.add_argument("--no-asr", action="store_true",
                    help="关掉本地转录兜底（默认开启——它是覆盖率的保底）")
    ap.add_argument("--asr-model", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    eps = load_json(args.episodes, {}).get("episodes", [])
    if args.limit:
        eps = eps[: args.limit]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        recs = list(ex.map(lambda e: resolve_one(e, not args.no_asr,
                                                 args.asr_model, args.probe), eps))

    print(f"{'节目':26} {'来源':16} {'词数':>7}  标题")
    print("-" * 104)
    for r in recs:
        tag = r["source"] + ("*" if r.get("from_cache") else "")
        print(f"{r['show'][:25]:26} {tag:16} {r['words']:>7}  {r['title'][:44]}")

    from collections import Counter
    cnt = Counter(r["source"] for r in recs)
    print("\n来源分布：", dict(cnt))
    full = sum(1 for r in recs if r["source"] != "notes_only" and r["words"] > 1500)
    print(f"拿到可用全文：{full}/{len(recs)}")

    index = [{k: v for k, v in r.items() if k != "text"} for r in recs]
    save_json(args.out, {"generated_at": now_utc().isoformat(), "items": index})
    print(f"索引写入 {args.out}（正文在 cache/transcripts/）")


if __name__ == "__main__":
    main()
