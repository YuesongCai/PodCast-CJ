#!/usr/bin/env python3
"""公用工具：HTTP、HTML 清洗、日期解析、路径。"""
import hashlib
import json
import os
import re
import ssl
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "cache")


def _load_dotenv():
    """读项目根目录的 .env 到 os.environ（不覆盖已有值）。

    凭据不进仓库：webhook URL、open_id、Base token 都从这里来。
    仓库是公开的，硬编码 webhook 等于把群的写入权限公开。
    """
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()
for _d in (DATA, CACHE, os.path.join(CACHE, "transcripts"), os.path.join(CACHE, "audio")):
    os.makedirs(_d, exist_ok=True)

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=45, headers=None, retries=2):
    """带重试的 GET。

    大 feed（如 Invest Like the Best）实测会 IncompleteRead 中断，
    一次失败就把整档节目判成不可用，所以必须重试。
    """
    import time as _t
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except Exception as e:
            last = e
            # IncompleteRead 有时能拿到已读到的部分，长度够就用
            partial = getattr(e, "partial", None)
            if partial and len(partial) > 20000 and attempt == retries:
                return partial
            if attempt < retries:
                _t.sleep(1.5 * (attempt + 1))
    raise last


def http_json(url, timeout=45, headers=None):
    return json.loads(http_get(url, timeout, headers).decode("utf-8", "replace"))


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h[1-6])>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&nbsp;", " "), ("&mdash;", "—"), ("&rsquo;", "'"),
                 ("&ldquo;", '"'), ("&rdquo;", '"'), ("&hellip;", "…")):
        s = s.replace(a, b)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def parse_duration(s):
    """itunes:duration -> 秒。支持 '3600' / '1:02:03' / '12:34'。"""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return int(sec)


def ep_id(show, guid, title):
    h = hashlib.sha1(f"{show}|{guid or ''}|{title or ''}".encode()).hexdigest()[:16]
    return h


def now_utc():
    return datetime.now(timezone.utc)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else None


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
