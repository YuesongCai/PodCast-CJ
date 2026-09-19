#!/usr/bin/env python3
"""流水线测试。

只用标准库 unittest，不依赖 pytest——这套要能在任何一台干净机器上
`python3 -m unittest` 直接跑起来。

fixture 全部是合成的，不读 data/ 下的真实数据：那些文件是 gitignored
（第三方播客内容不进仓库），CI 上根本不存在。

覆盖重点是「错了会看起来像对的」那些地方：
  - judge 的校验层（编造 id / 漏集 / 全判 must_listen 都不会报错，只会悄悄毁掉产出）
  - Telegram 分页（切错会丢字符或产生非法 HTML，Telegram 返回 400）
  - 邮件 HTML 转义（标题里一个 & 就能让整封邮件渲染错位）
"""
import copy
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import common                      # noqa: E402
import deliver_email as EM         # noqa: E402
import deliver_telegram as TG      # noqa: E402
import judge as J                  # noqa: E402
import shows_table as ST           # noqa: E402


# ---------------------------------------------------------------- fixtures

def make_pack(n=8, notes_only_at=None):
    items = []
    for i in range(n):
        items.append({
            "section": "subscribed" if i < n // 2 else "watchlist",
            "episode_id": f"{i:016x}",
            "show": f"Show {i}",
            "title": f"Episode {i}",
            "published": "2026-09-18",
            "duration_min": str(30 + i),
            "link": f"https://example.com/{i}",
            "youtube": f"https://youtube.com/watch?v={i}" if i % 2 else None,
            "evidence_source": "notes_only" if i == notes_only_at else "youtube",
            "fidelity": "notes_only" if i == notes_only_at else "full",
            "notes": "show notes",
            "compression": {"original_words": 9000, "kept_words": 1800},
            "evidence": "evidence text " * 50,
        })
    return {"generated_at": "2026-09-19T00:00:00Z", "items": items}


def make_judgments(pack):
    """一份合法判断：英文、must_listen 约 25%，符合区分度和语言要求。"""
    items = []
    for n, p in enumerate(pack["items"]):
        v = "must_listen" if n < len(pack["items"]) // 4 else (
            "worth_skim" if n % 2 else "skip")
        items.append({
            "episode_id": p["episode_id"],
            "section": p["section"],
            "show": p["show"],
            "title": p["title"],
            "verdict": v,
            "score": 8 if v == "must_listen" else 4,
            "why": f"Listen for one reason: the capex guide is {n}0bps above consensus.",
            "key_points": ([] if p["fidelity"] == "notes_only"
                           else [f"Management guided FY26 margin to {n}% (+ve)."]),
            "read_across": "Positive read-across to the power supply chain.",
            "topics": ["AI Infrastructure"],
        })
    return {
        "pm_summary": ("Today's flow establishes the power constraint as a financeable "
                       "line item: 1) orbital DC breakeven at US$500/kg, 2) construction "
                       "payrolls +22k attributed to datacentre build."),
        "items": items,
        "gaps": ["AI Ascent: Spotify-exclusive, no public RSS."],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.pack = make_pack()
        self.j = make_judgments(self.pack)
        self.idx = {i["episode_id"]: i for i in self.pack["items"]}


# ---------------------------------------------------------------- judge

class TestJudgeValidation(Base):
    """校验层。每个用例都对应一种实际会毁掉 newsletter 的模型错误。"""

    def test_valid_passes(self):
        self.assertEqual(J.validate(self.j, self.pack), [])

    def _broken(self, mutate):
        j = copy.deepcopy(self.j)
        mutate(j)
        return J.validate(j, self.pack)

    def test_rejects_hallucinated_id(self):
        errs = self._broken(lambda j: j["items"][0].update(episode_id="ffffffffffffffff"))
        self.assertTrue(any("不在输入里" in e for e in errs), errs)

    def test_rejects_missing_episode(self):
        errs = self._broken(lambda j: j["items"].pop(2))
        self.assertTrue(any("漏了" in e for e in errs), errs)

    def test_rejects_duplicate_id(self):
        errs = self._broken(lambda j: j["items"].append(copy.deepcopy(j["items"][0])))
        self.assertTrue(any("重复" in e for e in errs), errs)

    def test_rejects_bad_verdict(self):
        errs = self._broken(lambda j: j["items"][1].update(verdict="maybe"))
        self.assertTrue(any("verdict" in e for e in errs), errs)

    def test_rejects_out_of_range_score(self):
        for bad in (0, 11, "8", None, 3.5):
            errs = self._broken(lambda j, b=bad: j["items"][1].update(score=b))
            self.assertTrue(any("score" in e for e in errs), f"score={bad!r} 未被拦住")

    def test_rejects_empty_why(self):
        errs = self._broken(lambda j: j["items"][2].update(why="   "))
        self.assertTrue(any("why" in e for e in errs), errs)

    def test_rejects_section_mismatch(self):
        errs = self._broken(lambda j: j["items"][0].update(section="watchlist"))
        self.assertTrue(any("section" in e for e in errs), errs)

    def test_rejects_all_must_listen(self):
        """21 集给 9 个 must_listen 等于没筛——这是最隐蔽的失效方式。"""
        errs = self._broken(
            lambda j: [x.update(verdict="must_listen") for x in j["items"]])
        self.assertTrue(any("等于没筛" in e for e in errs), errs)

    def test_allows_high_ratio_on_small_sample(self):
        """3 集里 2 集值得听是完全可能的，样本小就不该卡。"""
        pack = make_pack(3)
        j = make_judgments(pack)
        for x in j["items"][:2]:
            x["verdict"], x["score"] = "must_listen", 9
        self.assertEqual(J.validate(j, pack), [])

    def test_rejects_fabricated_key_points_for_notes_only(self):
        """没有正文却给了 key points = 在编造内容，比漏掉更危险。"""
        pack = make_pack(notes_only_at=1)
        j = make_judgments(pack)
        j["items"][1]["key_points"] = ["编造的点"]
        errs = J.validate(j, pack)
        self.assertTrue(any("编造" in e for e in errs), errs)

    def test_rejects_missing_headline(self):
        errs = self._broken(lambda j: j.update(pm_summary=""))
        self.assertTrue(any("pm_summary" in e for e in errs), errs)

    def test_accepts_legacy_field_names(self):
        """旧字段名仍是合法别名：sync_bitable 和历史文件还在用。"""
        j = copy.deepcopy(self.j)
        j["today_in_one_line"] = j.pop("pm_summary")
        for x in j["items"]:
            x["for_you"] = x.pop("read_across", "")
        self.assertEqual(J.validate(j, self.pack), [])

    def test_rejects_empty_items(self):
        self.assertTrue(J.validate({"items": []}, self.pack))
        self.assertTrue(J.validate({}, self.pack))
        self.assertTrue(J.validate([], self.pack))


class TestJudgeTopics(Base):
    """标签由模型给，取值受限——否则每天冒出新标签会把归档表筛乱。"""

    def test_rejects_unknown_topic(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["topics"] = ["Crypto"]
        errs = J.validate(j, self.pack)
        self.assertTrue(any("topics" in e for e in errs), errs)

    def test_rejects_too_many_topics(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["topics"] = ["Energy", "Macro & Rates", "Tech Strategy",
                                   "AI Governance"]
        errs = J.validate(j, self.pack)
        self.assertTrue(any("最多 3 个" in e for e in errs), errs)

    def test_allows_empty_and_missing_topics(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["topics"] = []
        self.assertEqual(J.validate(j, self.pack), [])
        j["items"][0].pop("topics")
        self.assertEqual(J.validate(j, self.pack), [])

    def test_model_topics_beat_keyword_rules(self):
        """实测过的坑：'lobbying a regulator' 被规则打成 AI 治理。
        模型给了标签就必须用模型的。"""
        import sync_bitable as SB
        item = {"title": "Trade body files competitiveness submission with the regulator",
                "why": "Governance and regulation of asset managers.",
                "key_points": [], "topics": ["Asset Management"]}
        self.assertEqual(SB.topics_for(item, {"show": "The Long-Short"}),
                         ["资管行业"])
        # 没给标签时才回落到规则
        del item["topics"]
        self.assertIn("AI 治理与监管", SB.topics_for(item, {"show": "The Long-Short"}))


class TestJudgeLanguage(Base):
    """产出要给二级买方读，必须通篇英文。中文混入是这次重定位最可能的回归。"""

    def test_rejects_chinese_in_pm_summary(self):
        j = copy.deepcopy(self.j)
        j["pm_summary"] = "今天的信息流主要围绕人工智能的电力约束展开，值得关注。"
        errs = J.validate(j, self.pack)
        self.assertTrue(any("通篇英文" in e for e in errs), errs)

    def test_rejects_chinese_in_item_fields(self):
        for field in ("why", "title", "read_across"):
            j = copy.deepcopy(self.j)
            j["items"][0][field] = "这一集非常值得听，讲了很多有价值的内容和观点。"
            errs = J.validate(j, self.pack)
            self.assertTrue(any("通篇英文" in e for e in errs),
                            f"{field} 里的中文没被拦住: {errs}")

    def test_rejects_chinese_in_key_points(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["key_points"] = ["管理层把全年利润率指引上调了两个百分点，超出预期。"]
        errs = J.validate(j, self.pack)
        self.assertTrue(any("通篇英文" in e for e in errs), errs)

    def test_tolerates_a_few_chinese_chars_in_quotes(self):
        """引述里带个中文公司名不该整轮重试。"""
        j = copy.deepcopy(self.j)
        j["items"][0]["key_points"] = ['Management cited 茅台 as the pricing anchor.']
        self.assertEqual(J.validate(j, self.pack), [])

    def test_english_output_passes(self):
        self.assertEqual(J.validate(self.j, self.pack), [])


class TestJudgeParsing(unittest.TestCase):
    """模型不一定听话只输出 JSON，抠取要兜住常见包裹方式。"""

    def test_extracts_from_wrappers(self):
        for raw in ('{"a": 1}',
                    '```json\n{"a": 1}\n```',
                    '```\n{"a": 1}\n```',
                    '好的，这是结果：\n{"a": 1}',
                    '{"a": 1}\n\n以上。'):
            self.assertEqual(J.extract_json(raw), {"a": 1}, raw)

    def test_raises_on_garbage(self):
        for raw in ("", "   ", "没有 JSON", None):
            with self.assertRaises(ValueError):
                J.extract_json(raw)

    def test_builds_message_with_roster(self):
        pack = make_pack(3)
        msg = J.build_user_message("规格正文", pack)
        # 花名册必须在，模型靠它知道一集都不能漏
        for it in pack["items"]:
            self.assertIn(it["episode_id"], msg)
        self.assertIn("规格正文", msg)

    def test_backend_autoselect(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertEqual(J.pick_backend("auto"), "claude-cli")
            os.environ["ANTHROPIC_API_KEY"] = "sk-test"
            self.assertEqual(J.pick_backend("auto"), "api")
            self.assertEqual(J.pick_backend("claude-cli"), "claude-cli")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old


# ---------------------------------------------------------------- Telegram

class TestTelegram(Base):
    ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}

    def pages(self, limit=TG.SAFE_LEN):
        return TG.paginate(TG._blocks(self.j, self.idx, "2026-09-19"), limit=limit)

    def test_pages_under_telegram_limit(self):
        for p in self.pages():
            self.assertLessEqual(len(p), TG.MAX_LEN)

    def test_html_tags_whitelisted_and_balanced(self):
        """非白名单标签或未闭合标签，Telegram 直接 400。"""
        for n, p in enumerate(self.pages()):
            stack = []
            for m in re.finditer(r"<(/?)([a-zA-Z-]+)[^>]*>", p):
                closing, tag = m.group(1), m.group(2).lower()
                self.assertIn(tag, self.ALLOWED, f"第{n}页非法标签 <{tag}>")
                if closing:
                    self.assertTrue(stack and stack[-1] == tag, f"第{n}页错配 </{tag}>")
                    stack.pop()
                else:
                    stack.append(tag)
            self.assertEqual(stack, [], f"第{n}页未闭合 {stack}")

    def test_escapes_special_chars(self):
        self.assertEqual(TG.esc("a & b < c > d"), "a &amp; b &lt; c &gt; d")
        # & 必须最先转，否则 &lt; 会被二次转义成 &amp;lt;
        self.assertEqual(TG.esc("<"), "&lt;")
        self.assertNotIn("&amp;lt;", TG.esc("<b>"))

    def test_title_with_ampersand_survives(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["title"] = "E&P 与 <script> 与 100% > 50%"
        pages = TG.paginate(TG._blocks(j, self.idx, "d"))
        blob = "".join(pages)
        self.assertIn("E&amp;P", blob)
        self.assertIn("&lt;script&gt;", blob)

    def test_pagination_is_lossless(self):
        """曾经的 bug：单行超长时被截断而不是切分，静默丢数据。"""
        for n, limit in ((20000, 1000), (9999, 4096), (500, 100), (100, 100)):
            pages = TG.paginate(["A" * n], limit=limit)
            self.assertEqual(len("".join(pages)), n, f"{n}@{limit} 丢了字符")
            self.assertTrue(all(len(p) <= limit for p in pages))

    def test_split_does_not_break_entities(self):
        text = "公司治理 &amp; 资本配置 " * 300
        joined = "".join(TG.paginate([text], limit=700))
        self.assertEqual(re.findall(r"&(?!amp;|lt;|gt;)", joined), [])

    def test_never_splits_inside_a_block_when_it_fits(self):
        blocks = ["A" * 100, "B" * 100, "C" * 100]
        pages = TG.paginate(blocks, limit=250)
        # 每块要么完整出现在某一页里，要么该页放不下才换页
        for b in blocks:
            self.assertTrue(any(b in p for p in pages), "块被拦腰切开了")

    def test_send_without_config_fails_clearly(self):
        ok, info = TG.send(self.j, self.idx, "d", token="", chat_id="")
        self.assertFalse(ok)
        self.assertIn("TELEGRAM", info)


# ---------------------------------------------------------------- 邮件

class TestEmail(Base):
    def test_html_has_no_unescaped_user_text(self):
        j = copy.deepcopy(self.j)
        j["items"][0]["title"] = '<img src=x onerror=alert(1)> & "quoted"'
        html = EM.render_html(j, self.idx, "2026-09-19")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&amp;", html)

    def test_html_avoids_email_hostile_css(self):
        """flex/grid/position 在 Outlook 的 Word 引擎下会把版面打塌。"""
        html = EM.render_html(self.j, self.idx, "d")
        for bad in ("display:flex", "display:grid", "position:absolute",
                    "position:fixed"):
            self.assertNotIn(bad, html.replace(" ", ""))

    def test_html_is_self_contained(self):
        """邮件里不能有外链 CSS/JS：多数客户端会拦，拦掉就没样式了。"""
        html = EM.render_html(self.j, self.idx, "d")
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("<link", html.lower())

    def test_subject_carries_numbers(self):
        s = EM.subject(self.j, "2026-09-19")
        self.assertIn("09-19", s)
        self.assertIn("screened", s)
        self.assertIn("listen", s)
        self.assertLess(len(s), 100)     # 太长在手机收件箱会被截断
        self.assertFalse(re.search(r"[\u4e00-\u9fff]", s), "主题行不能有中文")

    def test_output_is_english(self):
        """整封邮件不能有中文——读者是二级买方，要的是研报。"""
        html = EM.render_html(self.j, self.idx, "2026-09-19")
        txt = EM.render_text(self.j, self.idx, "2026-09-19")
        for name, blob in (("html", html), ("text", txt)):
            found = re.findall(r"[\u4e00-\u9fff]", blob)
            self.assertEqual(found, [], f"{name} 里有中文: {''.join(found[:20])}")

    def test_uses_research_note_vocabulary(self):
        html = EM.render_html(self.j, self.idx, "d")
        for token in ("PODCAST SCREEN", "PM SUMMARY", "LISTEN",
                      "SCREENED OUT", "READ-ACROSS"):
            self.assertIn(token, html, f"缺分栏 {token}")

    def test_no_cross_show_essay_section(self):
        """跨节目 debates 已撤：那是分析不是筛选。历史文件里带着也不该渲染。"""
        j = copy.deepcopy(self.j)
        j["debates"] = [{"question": "Q#1: manufactured theme?",
                         "conclusion": "should not render",
                         "detail": "should not render", "shows": ["Show 0"]}]
        for blob in (EM.render_html(j, self.idx, "d"),
                     EM.render_text(j, self.idx, "d"),
                     "".join(TG.paginate(TG._blocks(j, self.idx, "d")))):
            self.assertNotIn("should not render", blob)
            self.assertNotIn("DEBATE", blob.upper())

    def test_legacy_debates_do_not_break_validation(self):
        j = copy.deepcopy(self.j)
        j["cross_cutting"] = [{"theme": "旧字段", "detail": "历史文件里有"}]
        self.assertEqual(J.validate(j, self.pack), [])

    def test_email_safe_fonts_only(self):
        """邮件里 @font-face 基本无效，只能用全平台自带字体。"""
        html = EM.render_html(self.j, self.idx, "d")
        self.assertNotIn("@font-face", html)
        self.assertNotIn("fonts.googleapis", html)
        self.assertIn("Georgia", html)

    def test_plain_text_alternative_covers_every_episode(self):
        txt = EM.render_text(self.j, self.idx, "d")
        for x in self.j["items"]:
            self.assertIn(x["title"], txt)

    def test_recipients_parsing(self):
        self.assertEqual(EM._recipients("a@b.c, d@e.f;g@h.i"),
                         ["a@b.c", "d@e.f", "g@h.i"])
        self.assertEqual(EM._recipients(""), [])
        self.assertEqual(EM._recipients(None), [])

    def test_send_reports_missing_config(self):
        ok, info = EM.send(self.j, self.idx, "d",
                           cfg={"host": "", "user": "", "password": ""})
        self.assertFalse(ok)
        self.assertIn("缺配置", info)

    def test_dry_run_assembles_without_network(self):
        ok, info = EM.send(self.j, self.idx, "2026-09-19", dry=True, cfg={
            "host": "smtp.example.com", "user": "u@e.com",
            "password": "x", "to": "a@b.c"})
        self.assertTrue(ok, info)
        self.assertIn("a@b.c", info)

    def test_notes_only_is_flagged_in_html(self):
        pack = make_pack(notes_only_at=0)
        j = make_judgments(pack)
        idx = {i["episode_id"]: i for i in pack["items"]}
        html = EM.render_html(j, idx, "d")
        self.assertIn("no transcript", html)


# ---------------------------------------------------------------- 其它

class TestShowsTable(unittest.TestCase):
    def test_flags_low_confidence_match(self):
        """名字对不上的 feed 必须被标出来——张冠李戴比没抓到更糟。"""
        feeds = [
            {"display": "AI Ascent", "itunes_name": "ASCENT",
             "itunes_author": "别人", "feed": "https://x/1", "match_score": 40},
            {"display": "Acquired", "itunes_name": "Acquired",
             "itunes_author": "Ben & David", "feed": "https://x/2", "match_score": 140},
        ]
        rows, missing = ST.build(feeds, [], ["AI Ascent", "Acquired"])
        self.assertEqual(rows[0]["你给的名字"], "AI Ascent")   # 可疑的排最前
        self.assertEqual(rows[0]["需核对"], "⚠️")
        self.assertEqual(rows[1]["需核对"], "")

    def test_reports_unresolved_seed_entries(self):
        rows, missing = ST.build([], [], ["Stratechery"])
        self.assertEqual(missing, ["Stratechery"])

    def test_markdown_escapes_pipes(self):
        """节目名里带 | 会把 Markdown 表格切坏。"""
        feeds = [{"display": "A|B", "itunes_name": "A|B", "feed": "u",
                  "match_score": 140}]
        rows, _ = ST.build(feeds, [], [])
        md = ST.to_markdown(rows, "t")
        self.assertIn("A\\|B", md)


class TestFeedResolution(unittest.TestCase):
    """节目名 -> RSS 的匹配打分。抓的是张冠李戴，比抓不到更糟。"""

    def setUp(self):
        import resolve_feeds
        self.R = resolve_feeds

    def test_cjk_survives_normalisation(self):
        """原来的 [^a-z0-9] 会把中文抹成空串，于是所有中文节目名彼此「相等」拿满分。
        实测后果：「科技早知道」被错配成《科幻新闻早知道》。"""
        self.assertIn("科技早知道", self.R.norm("What's Next｜科技早知道"))
        self.assertNotEqual(self.R.norm("科技早知道"), "")
        self.assertNotEqual(self.R.norm("科技早知道"),
                            self.R.norm("科幻新闻早知道"))

    def test_picks_correct_chinese_show_over_decoy(self):
        right = {"collectionName": "What's Next｜科技早知道",
                 "artistName": "声动活泼", "feedUrl": "https://x/1"}
        decoy = {"collectionName": "《科幻新闻早知道》｜全球科技科幻新闻直通车",
                 "artistName": "科幻深读小峰", "feedUrl": "https://x/2"}
        s_right = self.R.score(right, "科技早知道", "声动活泼")
        s_decoy = self.R.score(decoy, "科技早知道", "声动活泼")
        self.assertGreater(s_right, s_decoy + 40,
                           f"正确 {s_right} vs 干扰 {s_decoy}，区分度不够")

    def test_empty_normalisation_is_penalised(self):
        """两边归一化后都为空不能算匹配。"""
        blank = {"collectionName": "!!! ???", "artistName": "@@@",
                 "feedUrl": "https://x/3"}
        self.assertLess(self.R.score(blank, "###", "$$$"), 0)

    def test_missing_feed_url_is_disqualifying(self):
        no_feed = {"collectionName": "Odd Lots", "artistName": "Bloomberg"}
        self.assertLess(self.R.score(no_feed, "Odd Lots", "Bloomberg"), 0)

    def test_chinese_tokenisation_is_per_character(self):
        self.assertEqual(self.R._tokens("科技早知道"), list("科技早知道"))
        self.assertEqual(self.R._tokens("odd lots"), ["odd", "lots"])
        self.assertEqual(self.R._tokens("odd 早知道"), ["odd"] + list("早知道"))


class TestTranscriptLanguageGuard(unittest.TestCase):
    """英文 ASR 模型喂中文音频会输出音译噪音——长度正常、语法通顺、内容全是编的。
    这比拿不到正文危险得多：拿不到会标 notes_only 并禁止编造 key points，
    而噪音会让判断环节煞有介事地胡说，读者看不出来。"""

    def setUp(self):
        import get_transcript
        self.G = get_transcript
        self.zh_ep = {"show": "科技早知道",
                      "title": "Snap 做了十年眼镜，终于等到它的时代了吗？",
                      "notes": "本期我们聊聊 Snap 的智能眼镜业务"}
        self.en_ep = {"show": "Odd Lots", "title": "A Goldman M&A Banker",
                      "notes": "Tracy and Joe talk to Gene Sykes"}

    def test_rejects_transliterated_noise(self):
        """实测样本：62 分钟中文节目转出 7263 字符、0 个中文字符。"""
        noise = ("T minus CO, that means Hasabi's Jose and I don't know Jan "
                 "Hoshua. Well, each away AI should and five Junji Gongji. ") * 40
        self.assertIsNotNone(self.G.language_mismatch(noise, self.zh_ep))

    def test_accepts_real_chinese_transcript(self):
        good = "我们今天聊聊 Snap 这家公司的智能眼镜业务，过去十年它做了什么。" * 40
        self.assertIsNone(self.G.language_mismatch(good, self.zh_ep))

    def test_ignores_english_shows(self):
        en = "Tracy and Joe talk to Gene Sykes about the Olympics bid. " * 40
        self.assertIsNone(self.G.language_mismatch(en, self.en_ep))

    def test_routes_chinese_audio_to_whisper(self):
        engine, model, lang = self.G.asr_engine_for(self.zh_ep)
        self.assertEqual(engine, "whisper")
        self.assertEqual(lang, "zh")
        self.assertIn("whisper", model)

    def test_routes_english_audio_to_parakeet(self):
        engine, _, lang = self.G.asr_engine_for(self.en_ep)
        self.assertEqual(engine, "parakeet")
        self.assertIsNone(lang)

    def test_mixed_title_with_mostly_english_stays_parakeet(self):
        """偶尔一个中文词不该把整档切到慢引擎。"""
        ep = {"show": "Acquired", "title": "TSMC: the 台积电 story",
              "notes": "Ben and David on the semiconductor giant's history " * 5}
        self.assertEqual(self.G.asr_engine_for(ep)[0], "parakeet")


class TestFetchWindows(unittest.TestCase):
    """回看窗口按节目自己的更新节奏推导。窗口管覆盖率，tier 管判断，两件事。"""

    def setUp(self):
        import fetch_episodes
        self.F = fetch_episodes
        from datetime import datetime, timedelta, timezone
        self.now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        self.td = timedelta

    def dates(self, *day_offsets):
        return [self.now - self.td(days=d) for d in day_offsets]

    def test_same_day_pairs_do_not_collapse_the_median(self):
        """Capital Cycle 每次同一天连发两集，间隔序列里一半是 0。
        不按发布日去重的话中位数被压成 0，月更节目窗口算成 2 天、永远抓不到。"""
        pub = []
        for d in (22, 50, 81, 113):
            pub += self.dates(d, d)          # 每个日期两集
        win, basis = self.F.effective_window("fresh", pub, 2.0)
        self.assertGreater(win, 25, f"窗口 {win} 天接不住月更节目（{basis}）")

    def test_infrequent_but_time_sensitive_show_is_still_reachable(self):
        """TCW 90 天只发 2 集却是高时效。tier 不该把它的窗口压到抓不到。"""
        win, _ = self.F.effective_window("fresh", self.dates(10, 42, 75), 2.0)
        self.assertGreaterEqual(win, 12)

    def test_evergreen_floor_applies_to_frequent_shows(self):
        """长青档就算天天更新也该多看几天。"""
        win, basis = self.F.effective_window(
            "evergreen", self.dates(0, 1, 2, 3, 4, 5), 2.0)
        self.assertGreaterEqual(win, 14)
        self.assertIn("下限", basis)

    def test_window_is_capped(self):
        win, _ = self.F.effective_window("evergreen", self.dates(1, 200, 400), 2.0)
        self.assertLessEqual(win, self.F.MAX_WINDOW_DAYS)

    def test_no_cadence_data_falls_back_to_tier_floor(self):
        win, basis = self.F.effective_window("semi", [], 2.0)
        self.assertEqual(win, 5.0)
        self.assertIn("无节奏", basis)

    def test_unknown_tier_defaults_conservatively(self):
        win, _ = self.F.effective_window("nonsense", self.dates(0, 1, 2), 2.0)
        self.assertLessEqual(win, 10)


class TestCommon(unittest.TestCase):
    def test_parse_duration_forms(self):
        self.assertEqual(common.parse_duration("3600"), 3600)
        self.assertEqual(common.parse_duration("1:02:03"), 3723)
        self.assertEqual(common.parse_duration("12:34"), 754)
        self.assertIsNone(common.parse_duration(""))
        self.assertIsNone(common.parse_duration("abc"))

    def test_parse_date_forms(self):
        for s in ("Wed, 06 Aug 2026 14:00:00 GMT", "2026-08-06T14:00:00Z",
                  "2026-08-06"):
            d = common.parse_date(s)
            self.assertIsNotNone(d, s)
            self.assertIsNotNone(d.tzinfo, f"{s} 缺时区，跨时区比较会错一天")
        self.assertIsNone(common.parse_date("不是日期"))

    def test_ep_id_is_stable_and_distinct(self):
        a = common.ep_id("Show", "guid-1", "Title")
        self.assertEqual(a, common.ep_id("Show", "guid-1", "Title"))
        self.assertNotEqual(a, common.ep_id("Show", "guid-2", "Title"))
        self.assertEqual(len(a), 16)

    def test_strip_html_unescapes_entities(self):
        out = common.strip_html("<p>a &amp; b</p><br/><script>x()</script>c")
        self.assertIn("a & b", out)
        self.assertNotIn("x()", out)


class TestDeliverOrchestration(unittest.TestCase):
    """渠道探测：没配的渠道要跳过，不能算失败。"""

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in (
            "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "LARK_WEBHOOK",
            "LARK_USER_ID")}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_detects_each_channel(self):
        import deliver as D
        self.assertFalse(D.configured("email"))
        self.assertFalse(D.configured("telegram"))
        os.environ.update(SMTP_HOST="h", SMTP_USER="u", SMTP_PASS="p",
                          EMAIL_TO="a@b.c")
        self.assertTrue(D.configured("email"))
        os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
        self.assertTrue(D.configured("telegram"))
        os.environ["LARK_WEBHOOK"] = "https://x"
        self.assertTrue(D.configured("lark_webhook"))

    def test_partial_email_config_is_not_configured(self):
        import deliver as D
        os.environ.update(SMTP_HOST="h", SMTP_USER="u")    # 少 SMTP_PASS
        self.assertFalse(D.configured("email"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
