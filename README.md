# Podcast 每日 Newsletter

每天早上一份能 scroll 一遍的播客早报：**订阅列表当天更新的判断（值不值得听）** + **盲区覆盖**。

核心不是内容总结，是**筛选**。所有实现选择都围绕一件事：把 transcript 拿到手，且拿得干净、可靠、覆盖广。

---

## 一、结论先说：transcript 怎么来的

这是整个项目唯一的难点。实测下来是一条**分层来源链**，命中即停：

| 层 | 来源 | 成本 | 覆盖 | 实测结论 |
|---|---|---|---|---|
| 1 | RSS 描述内嵌全文 | 免费 | 1 档 | Latent Space 把 Substack 全文塞进 RSS，128K 字符直接可用 |
| 2 | RSS `<podcast:transcript>` 标签 | 免费 | 1 档 | 25 档里只有 Short Briefings 有（53% 集）。**指望这个是死路** |
| 3 | YouTube 自动字幕 | 免费 | 大多数 | 主力来源。免登录可取，5K–32K 词/集 |
| 4 | 节目官网正文 | 免费 | ~0 | **基本无用**，见下方「踩过的坑」 |
| 5 | 本地 ASR 转录 RSS 音频 | 算力 | **100%** | 覆盖率兜底。M3 上 12–25x 实时，11 分钟音频 28–57 秒出结果 |

第 2 层补充：订阅列表里只有 Short Briefings 有官方 transcript，但 watchlist 里
**Bloomberg 系（Bloomberg Surveillance、Odd Lots）自己在 RSS 里发全文**，
带说话人标签，质量比自动字幕好。所以「有没有官方 transcript」要逐档试，不能按印象判断。

**为什么不用付费源**：查证过 Podscan $100/月那档之所以"全"，是因为它自己对全网 RSS 音频跑 ASR。
也就是说付费买到的和第 5 层是同一个东西——都是从 RSS 音频转录。而付费也补不了
AI Ascent（Spotify 独占，无公开 RSS 可索引）。缺口只有约 19 集/月，本地转录合每月 ~55 分钟算力，
所以钱留着。（Taddy 生成额度 Pro 仅 100 条/月；Listen Notes 官方 transcript 覆盖率 <1%，两者都不解决问题。）

### 实测覆盖率

2026-08-08 的实跑结果（窗口内全部新集）：

| | 集数 | 拿到全文 | 来源分布 |
|---|---|---|---|
| 订阅列表（27 档） | 11 | **11 / 11** | YouTube 字幕 7、本地转录 4 |
| 盲区 watchlist（14 档） | 10 | **10 / 10** | YouTube 字幕 3、本地转录 4、官方 transcript 3 |
| 合计 | 21 | **21 / 21 = 100%** | |

只走免费快路径（第 1–4 层、不转录）时是 7/11；本地转录把剩下 4 集补齐。

拿不到的只剩 **AI Ascent**——Spotify 独占，Apple 和公开 RSS 都查不到，付费源同样无解。

---

## 二、两个必须知道的正确性设计

### 1. YouTube 匹配的双重校验（防张冠李戴）

搜到的视频必须**同时**满足「标题相似度 ≥ 0.55」和「时长与 RSS 相差 ≤ 8%」才采用。

这条拦住的真实案例：`In Good Company` 的 RSS 是 11 分钟 HIGHLIGHTS 版，
YouTube 上是 60 分钟正片，标题相似度 1.00 但时长差 246.7% —— **被正确拒绝**。
如果只看标题，就会用正片内容去总结一个 highlights 集。

同时必须先锁定节目自己的 YouTube 频道再在频道内搜。实测 YC 的
`Garry Tan: Own Your Intelligence` 全局搜前 5 条里根本没有正片，频道内搜第一条就是它、时长差 0.3%。

### 2. 官网抓取必须看结构，不能看长度

最初用「网页正文比 show notes 长 3 倍」判定"这页有 transcript"，结果全军覆没：

- `In Good Company` → 抓到 Acast 播放器页的导航（"Follow Share Play"）
- `Garry Tan` → 抓到 Spotify 页面上**整档节目其它集的简介列表**，6203 词全是别集内容

拿这个喂 AI 会产出**煞有介事的胡说**。现在改成：播放器/托管域名直接跳过 +
`transcript_likeness()` 结构判据（口语特征词密度、句长分布、短碎片行占比）。
代价是覆盖率下降，但错的内容比没有内容危险得多。

---

## 三、成本控制：抽取式压缩

一集 Cognitive Revolution 有 27800 词。判断"值不值得听"不需要通读全文，
所以 `build_digest.py` 用**纯代码**（零 LLM 成本）把每集压到 ~1800 词：

- 开头 450 词原文（主播通常在这里交代主旨和嘉宾）
- 结尾 250 词原文（结论）
- 中段按信息密度选句：带数字/金额/百分比、专有名词、观点动词（think/argue/because…）优先，**保持原文顺序**

LLM 只做筛选和判断——正是这个需求的价值所在。

---

## 四、目录结构

```
pipeline/
  common.py            公用工具（HTTP / HTML清洗 / 日期 / 缓存路径）
  resolve_feeds.py     节目名 -> RSS feed（iTunes Search API，免费无 key）
  scan_feeds.py        feed 体检：更新频率、有无官方 transcript、notes 厚度
  resolve_youtube.py   节目 -> YouTube 频道（带时长校验，缓存）
  fetch_episodes.py    拉窗口内新集，过滤 highlights/预告，切掉法律免责声明
  get_transcript.py    ★ 五层来源链 + 双重校验
  asr_local.py         本地 ASR（跑在 .venv，Parakeet MLX）
  build_digest.py      抽取式压缩 -> 证据包
  coverage_report.py   逐档节目的 transcript 可得性
  render_newsletter.py 排版 + 飞书推送（群 webhook 交互卡片 / 私聊带附件）
  sync_bitable.py      同步到飞书多维表格（按 episode_id 去重，可重复跑）
  charts_radar.py      Apple 榜单雷达（已降级为可选，见下）

prompts/
  daily_digest_prompt.md   ★ Claude 的判断规格（筛选逻辑在这）

data/
  shows_seed.txt        订阅列表（CJ 的 27 档，可直接编辑）
  watchlist_seed.txt    盲区 watchlist（可直接编辑）
  feeds.json            解析结果
  digest_pack.json      给 Claude 的证据包
  judgments.json        Claude 的判断
newsletters/            产出的 md
cache/transcripts/      正文缓存（按 episode id）
```

## 五、跑起来

```bash
./run_daily.sh
```

跑完停在 `data/digest_pack.json`。然后让 Claude 读 `prompts/daily_digest_prompt.md`
产出 `data/judgments.json`，最后：

```bash
python3 pipeline/render_newsletter.py --webhook && python3 pipeline/sync_bitable.py
```

`--webhook` 发群交互卡片；加 `--send` 还会私聊发一份 md 附件。

### 多维表格

判断结果会累积到飞书多维表格，22 个字段，含判定、评分、主题标签，以及抓取侧的
元数据（正文来源、可信度、原始/压缩词数、广告切除量）——后者是为了以后能回答
「哪档节目老是拿不到全文」这类问题。

按 `episode_id` 去重，重复跑只会更新不会新增；发现历史重复行会自动清理。
首次初始化用 `python3 pipeline/sync_bitable.py --init`——没配 `LARK_BASE_TOKEN`
时会自动新建一个 Base 并把 token 写回 `data/bitable.json`。

### 凭据配置

飞书相关的凭据全部走 `.env`（已在 `.gitignore` 里）：

```bash
cp .env.example .env   # 然后填入自己的 webhook / open_id / Base token
```

**不要把 webhook URL 提交到仓库**——它是群的写入凭据，泄漏后任何人都能往群里发消息。

**主题标签由 Claude 在出判断时直接给**，不用关键词规则。早先用正则打标的教训：
「差的**治理**」（公司治理）被打成「AI 治理与监管」，否定句「跟**配置**没有交集」
被打成「资管行业」，「劳动力**指标**」被打成「交易与风控」。规则只留作兜底。

### 初始化（只需一次）

```bash
brew install ffmpeg
uv tool install yt-dlp
uv venv .venv --python 3.12 && .venv/bin/python -m pip install parakeet-mlx
python3 pipeline/resolve_feeds.py data/shows_seed.txt data/feeds.json
python3 pipeline/resolve_feeds.py data/watchlist_seed.txt data/watchlist_feeds.json
python3 pipeline/resolve_youtube.py
```

## 六、增删节目

编辑 `data/shows_seed.txt`（一行一档：`显示名|作者|搜索关键词`），然后：

```bash
python3 pipeline/resolve_feeds.py data/shows_seed.txt data/feeds.json && python3 pipeline/resolve_youtube.py
```

---

## 七、已知限制（不粉饰）

1. **AI Ascent 拿不到**。Spotify 独占，Apple 无收录，无公开 RSS。付费源也无解。
2. **Stratechery 用公开替代**。播客正片是 Passport 订阅者私有 RSS；已换成
   Ben Thompson 的公开节目 Exponent + Sharp Tech（观点来源相同，不是同一档节目）。
3. **YouTube 会瞬时限流**。搜索偶尔返回空结果，已加重试；但这也是为什么本地 ASR 必须是主干而非备选。
4. **榜单发现（`charts_radar.py`）没有实用价值**。实测 Apple Business/Technology 榜前列是
   Crime Junkie、Habits and Hustle、Coffeez with Joe Shalaby 这类内容——榜单优化的是大众流行度，
   不是"不听会后悔"。盲区覆盖靠 `watchlist_seed.txt` 策展，脚本保留但不进日常流程。
5. **Bloomberg Surveillance 一天发 3 条**，信噪比低。已在流水线里限制 watchlist 单档每天最多 2 集，
   如果还嫌吵就从 watchlist 里删掉。
6. **`notes_only` 的集只能给判断，不给细节**。这类集在 newsletter 里会明确标注
   「仅凭 show notes」，prompt 里也硬性禁止编造 key points。
