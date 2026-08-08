#!/usr/bin/env python3
"""把每日判断同步到飞书多维表格。

设计意图：newsletter 推完就沉底了，判断结果应该可累积、可筛选、可回溯。
所以这张表存的是「每集一行 + 全部字段」，包括抓取侧的元数据
（正文来源、可信度、原始词数、广告切除量），这样以后能回答
「哪档节目老是拿不到全文」「哪类判定后来被证明看错了」这种问题。

去重靠 episode_id：同一集重复跑不会产生重复行（走 upsert）。

用法：
    python3 pipeline/sync_bitable.py --init      # 首次：建表+建字段
    python3 pipeline/sync_bitable.py             # 日常：同步当天判断
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, load_json, now_utc, save_json

LARK = os.environ.get("LARK_CLI", "/opt/homebrew/bin/lark-cli")
CONF_PATH = os.path.join(DATA, "bitable.json")

# Base token 从环境变量来（仓库公开，token 不进代码）。见 .env.example。
# 没设时用 --init 新建一个 Base。
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "")
LARK_DOMAIN = os.environ.get("LARK_DOMAIN", "https://feishu.cn")
TABLE_NAME = os.environ.get("LARK_BASE_TABLE", "每日判断")

SHOW_HUE = "Wathet"

# 主题标签：这是「足够好的分类」的核心——按投资视角切，不是按节目类型切
TOPICS = [
    ("宏观与利率", "Red"), ("资管行业", "Carmine"), ("能源", "Orange"),
    ("AI 基建与算力", "Purple"), ("AI 治理与监管", "Gray"), ("AI 应用层", "Blue"),
    ("交易与风控", "Turquoise"), ("创业与公司史", "Green"), ("消费与零售", "Yellow"),
    ("品牌与营销", "Lime"), ("科技战略", "Wathet"),
]

VALID_TOPICS = {n for n, _ in TOPICS}

VERDICT_OPTS = [("值得听", "Green"), ("扫一眼就够", "Yellow"), ("可以跳过", "Gray")]
SECTION_OPTS = [("订阅", "Blue"), ("盲区", "Purple")]
SOURCE_OPTS = [
    ("YouTube 字幕", "Red"), ("本地转录", "Purple"), ("RSS 内嵌全文", "Green"),
    ("官方 transcript", "Turquoise"), ("官网正文", "Wathet"), ("仅 show notes", "Gray"),
]
FIDELITY_OPTS = [("全文", "Green"), ("部分", "Yellow"), ("仅简介", "Gray")]

SOURCE_MAP = {"youtube": "YouTube 字幕", "asr": "本地转录", "rss_inline": "RSS 内嵌全文",
              "rss_transcript": "官方 transcript", "website": "官网正文",
              "notes_only": "仅 show notes"}
FIDELITY_MAP = {"full": "全文", "partial": "部分", "notes_only": "仅简介"}
VERDICT_MAP = {"must_listen": "值得听", "worth_skim": "扫一眼就够", "skip": "可以跳过"}
SECTION_MAP = {"subscribed": "订阅", "watchlist": "盲区"}


def run(args, timeout=180):
    p = subprocess.run([LARK] + args, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "").strip()
    try:
        # lark-cli 会在 JSON 后面追加 Tip 行，取第一个完整 JSON 对象
        start = out.index("{")
        depth, end = 0, None
        for i, ch in enumerate(out[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return json.loads(out[start:end]), p.returncode, (p.stderr or "")[-400:]
    except Exception:
        return None, p.returncode, (out + "\n" + (p.stderr or ""))[-500:]


def succeeded(d):
    """lark-cli 用 ok:true 表示成功；部分命令沿用 code:0。两种都认。"""
    if not isinstance(d, dict):
        return False
    if d.get("ok") is True:
        return True
    return d.get("code") in (0, None) and d.get("ok") is not False


def field_defs(shows):
    """字段定义。顺序即表里的列顺序。"""
    def sel(name, opts, multiple=False, desc=None):
        d = {"type": "select", "name": name, "multiple": multiple,
             "options": [{"name": n, "hue": h} for n, h in opts]}
        if desc:
            d["description"] = desc
        return d

    return [
        {"type": "text", "name": "标题", "description": "中文改写标题；不确定时用原标题"},
        sel("节目", [(s, SHOW_HUE) for s in shows]),
        sel("区块", SECTION_OPTS, desc="订阅=你自己订的；盲区=你没订但值得知道"),
        sel("判定", VERDICT_OPTS),
        {"type": "number", "name": "评分",
         "style": {"type": "rating", "icon": "star", "min": 1, "max": 10}},
        sel("主题标签", TOPICS, multiple=True, desc="按投资视角分类，不是按节目类型"),
        {"type": "datetime", "name": "发布日期", "style": {"format": "yyyy-MM-dd"}},
        {"type": "number", "name": "时长分钟", "style": {"type": "plain", "precision": 0}},
        {"type": "text", "name": "判断理由", "description": "为什么给这个判定——这是本表的核心价值"},
        {"type": "text", "name": "要点"},
        {"type": "text", "name": "对你的意义"},
        {"type": "text", "name": "原标题"},
        sel("正文来源", SOURCE_OPTS, desc="transcript 从哪来的，用于回溯覆盖率问题"),
        sel("正文可信度", FIDELITY_OPTS, desc="仅简介=没拿到全文，判断可能不准"),
        {"type": "number", "name": "原始词数", "style": {"type": "plain", "precision": 0}},
        {"type": "number", "name": "压缩后词数", "style": {"type": "plain", "precision": 0}},
        {"type": "number", "name": "广告切除词数", "style": {"type": "plain", "precision": 0}},
        {"type": "text", "name": "原页面", "style": {"type": "url"}},
        {"type": "text", "name": "YouTube", "style": {"type": "url"}},
        {"type": "text", "name": "音频链接", "style": {"type": "url"}},
        {"type": "text", "name": "episode_id", "description": "去重键，勿改"},
        {"type": "created_at", "name": "入库时间", "style": {"format": "yyyy-MM-dd HH:mm"}},
    ]


def do_init(shows):
    global BASE_TOKEN
    conf = load_json(CONF_PATH, {}) or {}
    if not BASE_TOKEN:
        BASE_TOKEN = conf.get("base_token") or ""
    if not BASE_TOKEN:
        # 没有现成 Base 就建一个
        d, rc, err = run(["base", "+base-create", "--name", "播客每日判断库",
                          "--time-zone", "Asia/Shanghai", "--as", "user"])
        b = ((d or {}).get("data") or {}).get("base") or {}
        BASE_TOKEN = b.get("base_token") or ""
        if not BASE_TOKEN:
            print("建 Base 失败，也没设 LARK_BASE_TOKEN:", err or d, file=sys.stderr)
            return 1
        conf["url"] = b.get("url") or f"{LARK_DOMAIN}/base/{BASE_TOKEN}"
        print(f"已新建 Base: {conf['url']}")
    conf["base_token"] = BASE_TOKEN
    conf.setdefault("url", f"{LARK_DOMAIN}/base/{BASE_TOKEN}")

    if not conf.get("table_id"):
        # 表可能已存在（重复 --init），先查
        dl, _, _ = run(["base", "+table-list", "--base-token", BASE_TOKEN, "--as", "user"])
        tid = None
        for t in ((dl or {}).get("data") or {}).get("tables", []):
            if t.get("name") == TABLE_NAME:
                tid = t.get("id")
        if not tid:
            d, rc, err = run(["base", "+table-create", "--base-token", BASE_TOKEN,
                              "--name", TABLE_NAME, "--as", "user"])
            if not succeeded(d):
                print("建表失败:", err or d, file=sys.stderr)
                return 1
            dd = d.get("data") or {}
            tid = dd.get("id") or dd.get("table_id") or (dd.get("table") or {}).get("id")
        if not tid:
            print("拿不到 table_id", file=sys.stderr)
            return 1
        conf["table_id"] = tid
        print(f"建表成功 table_id={tid}")
        save_json(CONF_PATH, conf)

    tid = conf["table_id"]
    d, rc, err = run(["base", "+field-list", "--base-token", BASE_TOKEN,
                      "--table-id", tid, "--as", "user"])
    existing = {}
    dd = (d or {}).get("data") or {}
    for f in (dd.get("fields") or dd.get("items") or []):
        nm = f.get("name") or f.get("field_name")
        if nm:
            existing[nm] = f.get("id") or f.get("field_id")
    print(f"现有字段 {len(existing)} 个: {list(existing)[:6]}")

    for spec in field_defs(shows):
        name = spec["name"]
        if name in existing:
            print(f"  = {name}（已存在，跳过）")
            continue
        d, rc, err = run(["base", "+field-create", "--base-token", BASE_TOKEN,
                          "--table-id", tid, "--json", json.dumps(spec, ensure_ascii=False),
                          "--as", "user"])
        okk = succeeded(d)
        print(f"  {'+' if okk else '!'} {name}" + ("" if okk else f"  -> {err or d}"))
    conf["fields_ready"] = True
    save_json(CONF_PATH, conf)
    print(f"\n表已就绪：{conf['url']}")
    return 0


def fetch_existing(bt, tid):
    """返回 (episode_id -> record_id, 重复的 record_id 列表)。

    record-list 的 JSON 结构是三个平行部分：fields 是列名、data 是按列序的行数组、
    record_id_list 是与 data 等长的 id 数组。JSON 里没有 _record_id 字段
    （只有 markdown 输出才有），所以必须靠 record_id_list 对齐。
    """
    existing, dupes = {}, []
    offset = 0
    while True:
        d, rc, err = run(["base", "+record-list", "--base-token", bt, "--table-id", tid,
                          "--format", "json", "--limit", "200", "--offset", str(offset),
                          "--as", "user"], timeout=300)
        dd = (d or {}).get("data") or {}
        cols = dd.get("fields") or []
        rows = dd.get("data") or []
        rids = dd.get("record_id_list") or []
        if not rows:
            break
        try:
            eid_i = cols.index("episode_id")
        except ValueError:
            break
        for row, rid in zip(rows, rids):
            eid = row[eid_i] if eid_i < len(row) else None
            if isinstance(eid, list):
                eid = "".join(x.get("text", "") for x in eid if isinstance(x, dict))
            if not eid or not rid:
                continue
            eid = str(eid).strip()
            if eid in existing:
                dupes.append(rid)          # 保留第一条，其余标记为重复
            else:
                existing[eid] = rid
        if not dd.get("has_more"):
            break
        offset += len(rows)
    return existing, dupes


def topics_for(item, meta):
    """按内容给主题标签。规则式打标，不够精细的地方由 judgments 里的文本兜底。"""
    show = (meta.get("show") or "").lower()
    blob = " ".join([item.get("title") or "", item.get("why") or "",
                     " ".join(item.get("key_points") or []),
                     item.get("for_you") or ""]).lower()
    t = set()
    def has(*ws):
        return any(w in blob for w in ws)

    if has("非农", "联储", "失业率", "降息", "利率", "国债", "refunding", "汇率", "久期"):
        t.add("宏观与利率")
    if has("aum", "资管", "管理人", "配置", "基金", "monetary authority", "mas", "aima", "税制"):
        t.add("资管行业")
    if has("油", "能源", "e&p", "天然气", "oil"):
        t.add("能源")
    if has("数据中心", "算力", "发射成本", "散热", "太空", "gpu", "能源瓶颈"):
        t.add("AI 基建与算力")
    if has("对齐", "监管", "治理", "安全", "限速", "pacing", "agi"):
        t.add("AI 治理与监管")
    if has("agent", "设计工具", "应用层", "html", "护城河"):
        t.add("AI 应用层")
    if has("风控", "仓位", "交易", "指标", "止损"):
        t.add("交易与风控")
    if has("创始人", "创业", "融资", "独角兽", "demo day", "公司史", "founder"):
        t.add("创业与公司史")
    if has("零售", "消费", "参与率", "品类", "客流"):
        t.add("消费与零售")
    if has("品牌", "粉丝", "营销", "体验"):
        t.add("品牌与营销")
    if "stratechery" in show or has("科技战略", "平台"):
        t.add("科技战略")
    return sorted(t)


def do_sync():
    conf = load_json(CONF_PATH, {}) or {}
    if not conf.get("table_id"):
        print("还没初始化，先跑 --init", file=sys.stderr)
        return 2
    tid = conf["table_id"]
    bt = BASE_TOKEN or conf["base_token"]

    j = load_json(os.path.join(DATA, "judgments.json"), None)
    if not j:
        print("读不到 judgments.json", file=sys.stderr)
        return 2
    pack = load_json(os.path.join(DATA, "digest_pack.json"), {}) or {}
    idx = {i["episode_id"]: i for i in pack.get("items", [])}
    eps = {}
    for p in ("episodes.json", "watchlist_episodes.json"):
        for e in (load_json(os.path.join(DATA, p), {}) or {}).get("episodes", []):
            eps[e["id"]] = e

    records = []
    for it in j.get("items", []):
        eid = it["episode_id"]
        m = idx.get(eid, {})
        e = eps.get(eid, {})
        c = m.get("compression") or {}
        pub = m.get("published") or e.get("published_date")
        f = {
            "标题": it.get("title") or m.get("title") or "",
            "节目": m.get("show") or it.get("show") or "",
            "区块": SECTION_MAP.get(it.get("section") or m.get("section"), "订阅"),
            "判定": VERDICT_MAP.get(it.get("verdict"), "扫一眼就够"),
            "判断理由": it.get("why") or "",
            "要点": "\n".join(f"• {k}" for k in (it.get("key_points") or [])),
            "对你的意义": it.get("for_you") or "",
            "原标题": m.get("title") or "",
            "正文来源": SOURCE_MAP.get(m.get("evidence_source"), "仅 show notes"),
            "正文可信度": FIDELITY_MAP.get(m.get("fidelity"), "仅简介"),
            "原页面": m.get("link") or e.get("link") or "",
            "YouTube": m.get("youtube") or "",
            "音频链接": e.get("audio_url") or "",
            "episode_id": eid,
        }
        if it.get("score"):
            f["评分"] = it["score"]
        if m.get("duration_min"):
            f["时长分钟"] = m["duration_min"]
        if pub:
            f["发布日期"] = f"{pub} 00:00:00"
        if c.get("original_words"):
            f["原始词数"] = c["original_words"]
        if c.get("kept_words"):
            f["压缩后词数"] = c["kept_words"]
        if c.get("ad_words_removed"):
            f["广告切除词数"] = c["ad_words_removed"]
        # 主题标签优先用 judgments 里给的（读过全文，准）；
        # 没给才退回关键词规则（实测规则会把「公司治理」误判成「AI 治理」、
        # 把否定句「跟配置没有交集」误判成「资管行业」，所以只当兜底）
        tp = [t for t in (it.get("topics") or []) if t in VALID_TOPICS]
        if not tp:
            tp = topics_for(it, m)
        if tp:
            f["主题标签"] = tp
        records.append({k: v for k, v in f.items() if v not in ("", None, [])})

    # 先拉已有记录，建 episode_id -> _record_id 映射，实现按业务键去重
    # （lark-cli 的 +record-upsert 只认 --record-id，不支持业务键 upsert）
    existing, dupes = fetch_existing(bt, tid)
    print(f"表中已有 {len(existing)} 条记录" +
          (f"，发现 {len(dupes)} 条重复待清理" if dupes else ""))
    if dupes:
        # 正确参数是 --json {"record_id_list": [...]}，不是 --record-ids；
        # 而且必须检查返回，否则删除静默失败、重复会一直累积
        gone = 0
        for i in range(0, len(dupes), 100):
            chunk = dupes[i:i + 100]
            d, rc, err = run(["base", "+record-delete", "--base-token", bt,
                              "--table-id", tid, "--json",
                              json.dumps({"record_id_list": chunk}),
                              "--as", "user", "--yes"], timeout=300)
            if succeeded(d):
                gone += len(chunk)
            else:
                print(f"  删除重复失败: {(err or str(d))[:200]}")
        print(f"  已删除 {gone}/{len(dupes)} 条重复记录")
        for e in dupes:
            pass

    to_create = [r for r in records if r["episode_id"] not in existing]
    to_update = [r for r in records if r["episode_id"] in existing]

    created = updated = failed = 0

    if to_create:
        cols = sorted({k for r in to_create for k in r})
        rows = [[r.get(c) for c in cols] for r in to_create]
        for i in range(0, len(rows), 200):            # 单批上限 200
            payload = {"fields": cols, "rows": rows[i:i + 200]}
            d, rc, err = run(["base", "+record-batch-create", "--base-token", bt,
                              "--table-id", tid, "--json",
                              json.dumps(payload, ensure_ascii=False),
                              "--as", "user"], timeout=300)
            if succeeded(d):
                created += len(rows[i:i + 200])
            else:
                failed += len(rows[i:i + 200])
                print(f"  批量新建失败: {(err or str(d))[:220]}")

    for r in to_update:
        rid = existing[r["episode_id"]]
        d, rc, err = run(["base", "+record-upsert", "--base-token", bt, "--table-id", tid,
                          "--record-id", rid, "--json",
                          json.dumps(r, ensure_ascii=False), "--as", "user"], timeout=180)
        if succeeded(d):
            updated += 1
        else:
            failed += 1
            print(f"  更新失败 {r['episode_id']}: {(err or str(d))[:180]}")

    print(f"同步完成：新增 {created}，更新 {updated}，失败 {failed}")
    print(f"表格：{conf.get('url')}")
    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="建表 + 建字段")
    args = ap.parse_args()

    shows = sorted({r["display"] for p in ("feeds.json", "watchlist_feeds.json")
                    for r in (load_json(os.path.join(DATA, p), []) or [])})
    return do_init(shows) if args.init else do_sync()


if __name__ == "__main__":
    sys.exit(main())
