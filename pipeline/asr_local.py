#!/usr/bin/env python3
"""本地 ASR：把播客音频转成文字。跑在 .venv 里（parakeet-mlx 装在那）。

用 NVIDIA Parakeet TDT 的 MLX 移植，Apple Silicon 上比 Whisper 快一个数量级，
英文准确率够用。长音频按块转，避免一次性吃满内存。

被 get_transcript.py 以子进程方式调用（主流水线跑系统 python，
parakeet 只在 venv 里，用子进程隔离比统一环境干净）。

单独用：
    .venv/bin/python pipeline/asr_local.py --audio-url <url> --out out.json
    .venv/bin/python pipeline/asr_local.py --wav /tmp/a.wav
"""
import argparse
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")
MODEL = os.environ.get("ASR_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
# 引擎二选一：
#   parakeet — 默认，英文/欧语，M3 上 12-25x 实时，最快
#   whisper  — 中文等非欧语必须走这条。parakeet 喂中文不会报错，
#              它会把中文**音译**成英文单词，产出一份读起来正常、内容全是编的转录
#              （实测：科技早知道 62 分钟 -> 7263 字符、0 个中文字符）。
ENGINE = os.environ.get("ASR_ENGINE", "parakeet").lower()
LANGUAGE = os.environ.get("ASR_LANGUAGE") or None   # whisper 用；留空则自动检测

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=900, context=_CTX) as r, open(dest, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def to_wav(src, dst):
    """转 16kHz 单声道 wav —— ASR 模型要求的输入格式。"""
    p = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-i", src, "-vn",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
        capture_output=True, timeout=1800)
    if p.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("ffmpeg 转码失败: " + p.stderr.decode("utf-8", "replace")[-300:])
    return os.path.getsize(dst)


def audio_seconds(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(p.stdout.strip())
    except Exception:
        return None


def transcribe_whisper(wav):
    """mlx-whisper。比 parakeet 慢，但中文/日文这些非欧语只能走它。"""
    import mlx_whisper
    kw = {"path_or_hf_repo": MODEL}
    if LANGUAGE:
        kw["language"] = LANGUAGE
    res = mlx_whisper.transcribe(wav, **kw)
    text = (res.get("text") or "").strip()
    segs = [{"start": round(s.get("start") or 0, 2),
             "end": round(s.get("end") or 0, 2),
             "text": (s.get("text") or "").strip()}
            for s in (res.get("segments") or [])]
    return text, segs


def transcribe(wav, chunk_min=2.0):
    if ENGINE == "whisper":
        return transcribe_whisper(wav)
    from parakeet_mlx import from_pretrained
    model = from_pretrained(MODEL)
    # 长音频分块：chunk_duration 秒，overlap 保证跨块句子不断
    res = model.transcribe(wav, chunk_duration=chunk_min * 60, overlap_duration=15)
    text = (res.text or "").strip()
    segs = []
    for s in (getattr(res, "sentences", None) or []):
        segs.append({"start": round(getattr(s, "start", 0) or 0, 2),
                     "end": round(getattr(s, "end", 0) or 0, 2),
                     "text": (getattr(s, "text", "") or "").strip()})
    return text, segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio-url")
    ap.add_argument("--wav")
    ap.add_argument("--out")
    ap.add_argument("--chunk-min", type=float, default=2.0)
    args = ap.parse_args()

    t0 = time.time()
    stats = {"model": MODEL, "engine": ENGINE}
    tmp = tempfile.mkdtemp(prefix="asr_")
    try:
        if args.wav:
            wav = args.wav
        else:
            if not args.audio_url:
                print(json.dumps({"ok": False, "error": "需要 --audio-url 或 --wav"}))
                return 2
            raw = os.path.join(tmp, "src.bin")
            nbytes = download(args.audio_url, raw)
            stats["downloaded_mb"] = round(nbytes / 1048576, 1)
            stats["download_sec"] = round(time.time() - t0, 1)
            wav = os.path.join(tmp, "a.wav")
            t1 = time.time()
            to_wav(raw, wav)
            stats["transcode_sec"] = round(time.time() - t1, 1)

        dur = audio_seconds(wav)
        stats["audio_sec"] = round(dur, 1) if dur else None

        t2 = time.time()
        text, segs = transcribe(wav, args.chunk_min)
        el = time.time() - t2
        stats["asr_sec"] = round(el, 1)
        if dur:
            stats["realtime_factor"] = round(dur / max(el, 0.01), 1)
        stats["words"] = len(text.split())
        stats["total_sec"] = round(time.time() - t0, 1)

        out = {"ok": bool(text and len(text.split()) > 100), "text": text,
               "segments": segs[:4000], "stats": stats}
        if not out["ok"]:
            out["error"] = f"转录结果过短（{stats['words']} 词）"
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            brief = dict(out)
            brief.pop("text", None)
            brief.pop("segments", None)
            brief["out"] = args.out
            print(json.dumps(brief, ensure_ascii=False))
        else:
            print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ok"] else 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}",
                          "stats": stats}, ensure_ascii=False))
        return 1
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
