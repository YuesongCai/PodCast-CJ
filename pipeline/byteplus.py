#!/usr/bin/env python3
"""BytePlus / 火山引擎 OpenAPI 客户端：签名 + 调用。

签名是 AWS SigV4 的变体，几个关键差异（踩过才知道）：
  - 头是 X-Date / X-Content-Sha256 / Authorization，不是 AWS 那套
  - scope 是 {date}/{region}/{service}/request，末尾是 `request` 不是 `aws4_request`
  - 算法串是 HMAC-SHA256，派生密钥从裸 SK 开始，没有 "AWS4" 前缀
  - query 要按 key 排序且逐个 percent-encode

SK 在控制台里是 base64 显示的，实际用哪一层要试——所以 sign() 允许传入任意一种，
probe 时三种都试一遍（见 __main__）。
"""
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get  # noqa: F401  (保证 .env 被加载)

AK = os.environ.get("BYTEPLUS_ACCESS_KEY_ID", "")
SK_RAW = os.environ.get("BYTEPLUS_SECRET_ACCESS_KEY", "")


def _sk_candidates(sk):
    """控制台显示的 SK 可能被 base64 包了一层或两层，签名时用哪层要试。"""
    import base64
    out = [sk]
    cur = sk
    for _ in range(2):
        try:
            dec = base64.b64decode(cur + "=" * (-len(cur) % 4)).decode("ascii")
            if dec and dec not in out and dec.isprintable():
                out.append(dec)
                cur = dec
            else:
                break
        except Exception:
            break
    return out


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _canon_query(params):
    # 必须按 key 排序，且每个 key/value 单独 quote（safe='-_.~'）
    items = sorted((str(k), str(v)) for k, v in params.items())
    return "&".join(f"{urllib.parse.quote(k, safe='-_.~')}="
                    f"{urllib.parse.quote(v, safe='-_.~')}" for k, v in items)


def sign_request(method, host, path, query, body, service, region, ak, sk,
                 extra_headers=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    xdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = xdate[:8]
    body = body or b""
    payload_hash = hashlib.sha256(body).hexdigest()

    headers = {
        "host": host,
        "x-date": xdate,
        "x-content-sha256": payload_hash,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v

    signed_keys = sorted(headers)
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in signed_keys)
    signed_headers = ";".join(signed_keys)

    canon_req = "\n".join([method, path, _canon_query(query), canon_headers,
                           signed_headers, payload_hash])
    scope = f"{datestamp}/{region}/{service}/request"
    to_sign = "\n".join(["HMAC-SHA256", xdate, scope,
                         hashlib.sha256(canon_req.encode()).hexdigest()])

    k = _hmac(sk.encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "request")
    sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (f"HMAC-SHA256 Credential={ak}/{scope}, "
                                f"SignedHeaders={signed_headers}, Signature={sig}")
    return headers


def call(action, version, service="iam", region="ap-singapore-1",
         host="open.byteplusapi.com", method="GET", params=None, body=None,
         ak=None, sk=None, timeout=40):
    """调一个 OpenAPI。返回 (status, parsed_or_text)。"""
    ak = ak or AK
    sk = sk or SK_RAW
    query = {"Action": action, "Version": version}
    query.update(params or {})
    raw = json.dumps(body).encode() if body is not None else b""
    extra = {"content-type": "application/json"} if body is not None else None
    headers = sign_request(method, host, "/", query, raw, service, region,
                           ak, sk, extra)
    url = f"https://{host}/?{_canon_query(query)}"
    req = urllib.request.Request(url, data=raw or None, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, json.loads(txt)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except json.JSONDecodeError:
            return e.code, txt[:400]
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:200]}"


if __name__ == "__main__":
    print(f"AK: {AK[:12]}…  SK 候选 {len(_sk_candidates(SK_RAW))} 种\n")
    for n, sk in enumerate(_sk_candidates(SK_RAW), 1):
        st, resp = call("ListUsers", "2018-01-01", service="iam", sk=sk)
        head = json.dumps(resp, ensure_ascii=False)[:180] if isinstance(resp, dict) else str(resp)[:180]
        print(f"SK#{n} ({sk[:16]}…) -> HTTP {st}  {head}")
