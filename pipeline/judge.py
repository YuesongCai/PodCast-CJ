#!/usr/bin/env python3
"""把证据包（digest_pack.json）送给 Claude，产出判断（judgments.json）。

这是流水线里**唯一**的非确定性环节，也因此是唯一需要硬约束的环节。
前面所有步骤（抓取/转录/压缩）都是纯代码，错了会报错；这一步错了会**看起来是对的**——
模型可能编一个不存在的 episode_id、漏掉几集、或者把 21 集全判成「值得听」。
那样的 newsletter 比没有更糟：它看起来完成了筛选，实际没有。

所以这里的结构是「调用 -> 校验 -> 把校验错误喂回去让它改 -> 再校验」，
校验不过就不落盘。校验规则见 validate()，每一条都对应一种实际会出的错。

两个后端：
  claude-cli  走本机 `claude -p`（复用 Claude Code 订阅授权，不需要额外 key）
  api         走 Anthropic API（需要 ANTHROPIC_API_KEY）
默认 auto：有 API key 用 api，否则用 CLI。

用法：
    python3 pipeline/judge.py                      # 自动选后端
    python3 pipeline/judge.py --backend api        # 强制走 API
    python3 pipeline/judge.py --dry-run            # 只打印将发送的 prompt 规模
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, ROOT, load_json, save_json

DEFAULT_PROMPT = os.path.join(ROOT, "prompts", "daily_digest_prompt.md")
DEFAULT_PACK = os.path.join(DATA, "digest_pack.json")
DEFAULT_OUT = os.path.join(DATA, "judgments.json")

# 判断质量就是这个产品的全部价值，所以默认用最强的模型。
# 想省钱改成 claude-sonnet-5：一次约 55K input tokens。
DEFAULT_MODEL = os.environ.get("PODCAST_JUDGE_MODEL", "claude-opus-5")

VERDICTS = {"must_listen", "worth_skim", "skip"}

# 主题标签由模型直接给（规则式打标会把「lobbying a regulator」误判成 AI 监管）。
# 这里卡住取值范围，免得每天冒出新标签把表筛乱。
TOPICS = {
    "Macro & Rates", "Asset Management", "Energy", "AI Infrastructure",
    "AI Governance", "AI Application Layer", "Trading & Risk",
    "Founders & Company History", "Consumer & Retail", "Brand & Marketing",
    "Tech Strategy",
}

# 21 集里给 9 个 must_listen 就等于没筛。prompt 里写了参考分布，
# 但模型不一定守，所以这里硬卡一道。样本太小（<6 集）时不卡，
# 因为 3 集里 2 集值得听是完全可能的。
MIN_ITEMS_FOR_DISTRIBUTION_CHECK = 6
MAX_MUST_LISTEN_RATIO = 0.6


# ---------------------------------------------------------------- 组 prompt

def build_user_message(prompt_spec, pack):
    """把任务说明和证据包拼成一条消息。

    刻意不让模型去读文件：headless 模式下给它文件工具意味着它可能去读
    仓库里别的东西，也意味着失败模式变多。证据直接内联，调用是纯函数。
    """
    items = pack.get("items", [])
    roster = "\n".join(
        f"  {i['episode_id']}  [{i.get('section')}]  {i.get('show')} — {i.get('title')}"
        for i in items
    )
    return (
        f"{prompt_spec}\n\n"
        "---\n\n"
        "# 本次输入\n\n"
        "下面是 `digest_pack.json` 的全部内容。你不需要读任何文件，证据都在这里。\n\n"
        f"## 必须覆盖的 {len(items)} 集（episode_id 照抄，一集都不能漏）\n\n"
        f"{roster}\n\n"
        "## 证据包\n\n"
        "```json\n"
        f"{json.dumps(pack, ensure_ascii=False, indent=1)}\n"
        "```\n\n"
        "---\n\n"
        "# 输出要求\n\n"
        "**只输出一个 JSON 对象**，不要 markdown 代码块，不要任何解释性文字，"
        "第一个字符必须是 `{`，最后一个字符必须是 `}`。\n"
        "结构严格按上面「输出格式」那节。不要写文件，直接把 JSON 作为回复内容输出。\n"
    )


# ---------------------------------------------------------------- 后端

def call_claude_cli(message, model, timeout):
    """走本机 claude CLI 的 headless 模式。

    prompt 走 stdin 而不是 argv：证据包 20 万字符级别，argv 有长度上限。
    --max-turns 1 + 不给工具，让它只能直接回答。
    """
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "1"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, input=message, capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("找不到 claude CLI。装 Claude Code，或改用 --backend api")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI 超时（{timeout}s）")

    if r.returncode != 0:
        raise RuntimeError(f"claude CLI 退出码 {r.returncode}: {(r.stderr or '')[-400:]}")

    try:
        env = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI 返回的不是 JSON: {r.stdout[:300]}")

    if env.get("is_error"):
        msg = env.get("result") or env.get("terminal_reason") or "未知错误"
        hint = ""
        if "authenticate" in str(msg).lower() or "oauth" in str(msg).lower():
            hint = "\n  -> 在终端跑一次 `claude` 重新登录，或配 ANTHROPIC_API_KEY 走 --backend api"
        raise RuntimeError(f"claude CLI 报错: {msg}{hint}")

    return env.get("result") or ""


def call_api(message, model, timeout):
    """走 Anthropic API。需要 ANTHROPIC_API_KEY。"""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("没装 anthropic SDK：pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("没有 ANTHROPIC_API_KEY，改用 --backend claude-cli")

    client = anthropic.Anthropic(timeout=timeout)
    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": message}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def pick_backend(name):
    if name != "auto":
        return name
    return "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-cli"


# ---------------------------------------------------------------- 解析 + 校验

def extract_json(text):
    """从模型回复里抠出 JSON 对象。

    即使要求了「只输出 JSON」，模型仍可能包一层 ```json 或者前面加一句话，
    所以这里做三级兜底：直接 parse -> 剥代码块 -> 取第一个 { 到最后一个 }。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回空内容")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    i, k = text.find("{"), text.rfind("}")
    if i >= 0 and k > i:
        return json.loads(text[i:k + 1])

    raise ValueError(f"回复里找不到 JSON 对象。开头 200 字符：{text[:200]}")


# 字段改过名（今日一句话 -> PM summary，交叉主题 -> debates，对你的意义 -> read-across）。
# 旧名保留为别名：sync_bitable 和历史 judgments.json 还在用，
# 而且模型偶尔会按记忆里的旧字段名输出，没必要为此整轮重试。
HEADLINE_KEYS = ("pm_summary", "today_in_one_line")
DEBATE_KEYS = ("debates", "cross_cutting")
READ_ACROSS_KEYS = ("read_across", "for_you")

# 产出要交给一个二级买方读，通篇必须是英文。
# 中文能混进来的两条路：模型按旧规格的记忆写，或者直接照抄了证据里的中文。
# 两种都会毁掉「研报」这个定位，所以单独卡一道。
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 允许极少量中文出现在引述里（比如节目里提到的中文公司名），超过阈值才判失败
MAX_CJK_CHARS_PER_FIELD = 8


def headline(j):
    for k in HEADLINE_KEYS:
        v = (j.get(k) or "").strip()
        if v:
            return v
    return ""


def debates(j):
    for k in DEBATE_KEYS:
        if isinstance(j.get(k), list):
            return j[k]
    return []


def read_across(x):
    for k in READ_ACROSS_KEYS:
        v = (x.get(k) or "").strip()
        if v:
            return v
    return ""


def _language_errors(j):
    """通篇英文。中文超过阈值的字段逐个点名，让模型知道改哪。"""
    errs = []

    def check(text, where):
        cjk = _CJK.findall(str(text or ""))
        if len(cjk) > MAX_CJK_CHARS_PER_FIELD:
            errs.append(f"{where}: 出现 {len(cjk)} 个中文字符，产出必须通篇英文"
                        f"（片段：{''.join(cjk[:12])}…）")

    check(headline(j), "pm_summary")
    for n, d in enumerate(debates(j)):
        if isinstance(d, dict):
            for k in ("question", "conclusion", "detail"):
                check(d.get(k), f"debates[{n}].{k}")
    for n, g in enumerate(j.get("gaps") or []):
        check(g, f"gaps[{n}]")
    for n, x in enumerate(j.get("items") or []):
        if not isinstance(x, dict):
            continue
        for k in ("title", "why", "read_across", "for_you"):
            check(x.get(k), f"items[{n}].{k}")
        for m, kp in enumerate(x.get("key_points") or []):
            check(kp, f"items[{n}].key_points[{m}]")
    # 单条报太多会把修复 prompt 撑爆，截断即可——模型看到模式就会整体改
    return errs[:10]


def validate(j, pack):
    """返回错误列表。空列表 = 通过。

    每条规则都对应一种实际会毁掉 newsletter 的错误，不是形式主义的 schema 检查：
    编造 id 会让这集在渲染时丢掉元数据；漏集等于没筛；
    notes_only 还给 key_points 就是在编造内容。
    """
    errs = []
    pack_items = {i["episode_id"]: i for i in pack.get("items", [])}

    if not isinstance(j, dict):
        return ["顶层不是 JSON 对象"]

    items = j.get("items")
    if not isinstance(items, list) or not items:
        return ["`items` 缺失或不是非空数组"]

    seen = set()
    for n, x in enumerate(items):
        where = f"items[{n}]"
        if not isinstance(x, dict):
            errs.append(f"{where}: 不是对象")
            continue

        eid = x.get("episode_id")
        if not eid:
            errs.append(f"{where}: 缺 episode_id")
            continue
        if eid not in pack_items:
            errs.append(f"{where}: episode_id `{eid}` 不在输入里（编造的 id，必须照抄输入）")
            continue
        if eid in seen:
            errs.append(f"{where}: episode_id `{eid}` 重复出现")
            continue
        seen.add(eid)

        src = pack_items[eid]
        label = f"{where} ({src.get('show')} — {str(src.get('title'))[:40]})"

        v = x.get("verdict")
        if v not in VERDICTS:
            errs.append(f"{label}: verdict `{v}` 非法，只能是 {sorted(VERDICTS)}")

        sc = x.get("score")
        if not isinstance(sc, int) or not 1 <= sc <= 10:
            errs.append(f"{label}: score `{sc}` 必须是 1–10 的整数")

        if not (x.get("why") or "").strip():
            errs.append(f"{label}: why 不能为空——skip 也要给具体理由")

        if x.get("section") != src.get("section"):
            errs.append(f"{label}: section 应为 `{src.get('section')}`，"
                        f"给的是 `{x.get('section')}`")

        tp = x.get("topics")
        if tp is not None:
            if not isinstance(tp, list):
                errs.append(f"{label}: topics 必须是数组")
            else:
                bad = [t for t in tp if t not in TOPICS]
                if bad:
                    errs.append(f"{label}: topics {bad} 不在固定取值里，"
                                f"只能用 {sorted(TOPICS)}")
                elif len(tp) > 3:
                    errs.append(f"{label}: topics 最多 3 个，给了 {len(tp)}")

        kp = x.get("key_points", [])
        if not isinstance(kp, list):
            errs.append(f"{label}: key_points 必须是数组")
        elif src.get("fidelity") == "notes_only" and kp:
            errs.append(f"{label}: fidelity 是 notes_only（没有正文），"
                        f"key_points 必须留空数组，现在有 {len(kp)} 条——这是在编造内容")

    missing = set(pack_items) - seen
    if missing:
        show = ", ".join(f"{m}({pack_items[m].get('show')})" for m in sorted(missing)[:8])
        errs.append(f"漏了 {len(missing)} 集没有判断，必须每集都出现："
                    f"{show}{' …' if len(missing) > 8 else ''}")

    # 区分度检查
    if len(items) >= MIN_ITEMS_FOR_DISTRIBUTION_CHECK and not errs:
        must = sum(1 for x in items if x.get("verdict") == "must_listen")
        ratio = must / len(items)
        if ratio > MAX_MUST_LISTEN_RATIO:
            errs.append(
                f"{len(items)} 集里给了 {must} 集 must_listen（{ratio:.0%}），等于没筛。"
                f"重新分档：真正有新增信息、反直觉、可操作的才给 must_listen，"
                f"参考分布 must_listen 两三成、skip 三四成")

    if not headline(j):
        errs.append("缺 pm_summary（当日 PM summary 段落）")

    for key in ("debates", "cross_cutting", "gaps"):
        if key in j and not isinstance(j[key], list):
            errs.append(f"`{key}` 必须是数组")

    errs.extend(_language_errors(j))
    return errs


# ---------------------------------------------------------------- 主流程

def judge(pack, prompt_spec, backend, model, timeout, max_repair, verbose=True):
    """调用 -> 校验 -> 修复。返回 (judgments, 尝试次数)。校验始终不过则抛异常。"""
    call = call_claude_cli if backend == "claude-cli" else call_api
    message = build_user_message(prompt_spec, pack)
    last_errs = []

    for attempt in range(max_repair + 1):
        if attempt:
            if verbose:
                print(f"  第 {attempt} 次修复，把 {len(last_errs)} 条校验错误喂回去")
            message = (
                build_user_message(prompt_spec, pack)
                + "\n\n---\n\n# 上一次的输出没通过校验\n\n"
                + "\n".join(f"- {e}" for e in last_errs)
                + "\n\n请重新输出**完整**的 JSON（不是补丁），修掉以上全部问题。\n"
            )

        if verbose:
            print(f"  调用 {backend} / {model}（{len(message):,} 字符）…", flush=True)
        raw = call(message, model, timeout)

        try:
            j = extract_json(raw)
        except ValueError as e:
            last_errs = [f"输出不是合法 JSON：{e}"]
            if verbose:
                print(f"  ✗ {last_errs[0]}")
            continue

        last_errs = validate(j, pack)
        if not last_errs:
            return j, attempt + 1
        if verbose:
            for e in last_errs[:6]:
                print(f"  ✗ {e}")
            if len(last_errs) > 6:
                print(f"  ✗ …另有 {len(last_errs) - 6} 条")

    raise RuntimeError(
        f"重试 {max_repair} 次后仍未通过校验，不落盘。最后的问题：\n"
        + "\n".join(f"  - {e}" for e in last_errs))


def main():
    ap = argparse.ArgumentParser(description="生成每日判断（digest_pack -> judgments）")
    ap.add_argument("--pack", default=DEFAULT_PACK)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "claude-cli", "api"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-repair", type=int, default=2,
                    help="校验失败后最多重试几次（默认 2）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 prompt 规模，不调用模型")
    args = ap.parse_args()

    pack = load_json(args.pack, None)
    if not pack or not pack.get("items"):
        print(f"读不到证据包或证据包为空：{args.pack}\n"
              f"先跑 ./run_daily.sh 生成", file=sys.stderr)
        return 2

    try:
        with open(args.prompt, encoding="utf-8") as f:
            spec = f.read()
    except OSError as e:
        print(f"读不到判断规格 {args.prompt}: {e}", file=sys.stderr)
        return 2

    items = pack["items"]
    n_sub = sum(1 for i in items if i.get("section") == "subscribed")
    print(f"证据包：{len(items)} 集（订阅 {n_sub} / 盲区 {len(items) - n_sub}）")

    if args.dry_run:
        msg = build_user_message(spec, pack)
        print(f"prompt {len(msg):,} 字符（约 {len(msg) // 3800:,}K tokens）")
        print(f"后端 {pick_backend(args.backend)} / 模型 {args.model}")
        return 0

    backend = pick_backend(args.backend)
    try:
        j, tries = judge(pack, spec, backend, args.model,
                         args.timeout, args.max_repair)
    except RuntimeError as e:
        print(f"\n判断失败：{e}", file=sys.stderr)
        return 1

    j.setdefault("_meta", {})
    j["_meta"].update({"backend": backend, "model": args.model,
                       "attempts": tries, "pack_generated_at": pack.get("generated_at")})
    save_json(args.out, j)

    dist = {}
    for x in j["items"]:
        dist[x["verdict"]] = dist.get(x["verdict"], 0) + 1
    print(f"\n✓ 校验通过（第 {tries} 次）-> {args.out}")
    print("  " + " / ".join(f"{k} {v}" for k, v in sorted(dist.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
