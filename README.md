# Podcast Screen — 每日播客筛选

给一份 podcast list，每天早上收到一封**英文买方投研语体**的邮件：
哪几集值得花时间、为什么、对持仓意味着什么，以及今天替你筛掉了什么。

核心不是内容总结，是**筛选**。所有实现选择都围绕两件事：
**把 transcript 拿到手**，以及**让判断真的有区分度**。

```bash
./run_daily.sh        # 抓取 -> 转录 -> 压缩 -> 判断 -> 渲染 -> 投递，无人值守
```

---

## 一、产出长什么样

收件人是**香港的二级市场买方**，整天读 sell-side research。所以产出是英文，
按研报的读法排版：conclusion-first、编号论据、`(+ve)/(-ve)` 方向标注、量化到数字。

```
PODCAST SCREEN                                              2026-09-19
─────────────────────────────────────────────────────────────────────
21 episodes screened (11 subscribed / 10 blind-spot) · 19h06m of audio
· 7 flagged to listen · 6h49m screened out

PM SUMMARY
Today's flow establishes the AI power constraint as a financeable line
item rather than a talking point, and it shows up in three independent
places: 1) Star Cloud puts orbital datacentre breakeven at US$500/kg
launch cost… 2) the negative July payroll had construction at +22k…
3) the only sector actually moving is utilities and IPPs (CEG, TLN, VST).

KEY DEBATES
Q#1: Is the binding constraint on AI compute power and cooling rather
     than silicon, and is it priced?
Conclusion: Evidence says power and cooling (+ve for IPPs and thermal
     supply chain); pricing is partial — visible in utilities, not in
     the payroll or capex read.
…

LISTEN                                                        7 of 21
BofA on the negative July payroll: the drag is seasonal and
sector-specific, the Fed's own metric still argues against urgency
Global Research Unlocked · 29min · LISTEN 8/10
Listen. Recorded roughly 90 minutes after the print, and it gives the
decomposition rather than the headline: 1) which line items dragged,
2) which reverse, and 3) which are genuine deterioration.
  • July payrolls turned negative… but the unemployment rate fell 0.1ppt (+ve)
  • Local government education roughly -50k, which BofA reads as seasonal…
  READ-ACROSS  The unemployment-rate-vs-own-forecast point is the
  cleanest argument against cut urgency. Relevant to duration and FX.
```

语体规格在 [`prompts/daily_digest_prompt.md`](prompts/daily_digest_prompt.md)，
改那个文件就能改产出的语气和侧重。

---

## 二、结论先说：transcript 怎么来的

这是整个项目唯一的技术难点。实测下来是一条**分层来源链**，命中即停：

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

## 三、三个必须知道的正确性设计

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

### 3. 判断环节的硬校验（`judge.py`）

流水线里前面每一步都是纯代码，错了会报错。**判断是唯一的非确定性环节，
而且错了会看起来是对的**——模型可能编一个不存在的 `episode_id`、漏掉几集、
或者把 21 集全判成「值得听」。那样的 newsletter 比没有更糟：它看起来完成了筛选，实际没有。

所以结构是「调用 → 校验 → 把校验错误喂回去让它改 → 再校验」，**校验不过就不落盘**：

| 校验 | 拦的是什么 |
|---|---|
| `episode_id` 必须在输入里 | 编造 id → 渲染时这集丢掉元数据 |
| 输入里每集都要有判断 | 漏集 = 没筛 |
| id 不重复 / `section` 与输入一致 | 错位到错误的分区 |
| `verdict` 取值合法、`score` 1–10 整数 | 渲染时静默降级 |
| `why` 非空（skip 也要给理由） | 「筛掉了什么」是产品的一半 |
| `must_listen` 占比 ≤ 60% | **21 集给 9 个 must_listen 等于没筛**，最隐蔽的失效方式 |
| `notes_only` 的集 `key_points` 必须为空 | 没有正文却给要点 = 编造内容 |
| `topics` 取值受限、最多 3 个 | 每天冒新标签会把归档表筛乱 |
| 通篇不得出现中文（阈值 8 字符） | 读者要的是英文研报 |

这 10 类都有对应的故障注入测试（`tests/test_pipeline.py`），56 项全绿。

### 4. 主题标签由模型给，不用关键词规则

规则式打标踩过的坑很具体：英文规则下，「trade body lobbying a **regulator**」（资管协会游说监管）
和「poor corporate **governance**」（油气公司治理差）都会被打成「AI 治理与监管」；
中文规则下，否定句「跟**配置**没有交集」被打成「资管行业」。

**词出现了不等于这集是讲那个的。** 现在标签由模型在出判断时直接给（取值受限的固定列表），
`topics_for()` 里的规则只在模型没给时兜底。

---

## 四、成本控制：抽取式压缩

一集 Cognitive Revolution 有 27800 词。判断"值不值得听"不需要通读全文，
所以 `build_digest.py` 用**纯代码**（零 LLM 成本）把每集压到 ~1800 词：

- 开头 450 词原文（主播通常在这里交代主旨和嘉宾）
- 结尾 250 词原文（结论）
- 中段按信息密度选句：带数字/金额/百分比、专有名词、观点动词（think/argue/because…）优先，**保持原文顺序**

21 集压完约 64K tokens，一次调用就能判完。LLM 只做筛选和判断——正是这个需求的价值所在。

---

## 五、目录结构

```
pipeline/
  common.py            公用工具（HTTP / HTML清洗 / 日期 / 缓存路径 / .env）
  resolve_feeds.py     节目名 -> RSS feed（iTunes Search API，免费无 key）
  shows_table.py     ★ 订阅列表核对表（抓张冠李戴用，见下）
  scan_feeds.py        feed 体检：更新频率、有无官方 transcript、notes 厚度
  resolve_youtube.py   节目 -> YouTube 频道（带时长校验，缓存）
  fetch_episodes.py    拉窗口内新集，过滤 highlights/预告，切掉法律免责声明
  get_transcript.py  ★ 五层来源链 + 双重校验
  asr_local.py         本地 ASR（跑在 .venv，Parakeet MLX）
  build_digest.py      抽取式压缩 -> 证据包
  judge.py           ★ 调模型出判断 + 硬校验 + 修复循环
  coverage_report.py   逐档节目的 transcript 可得性
  deliver.py         ★ 统一投递入口（多渠道、失败隔离）
  deliver_email.py     HTML 邮件（主渠道）
  deliver_telegram.py  Telegram bot
  render_newsletter.py markdown 归档 + 飞书交互卡片
  sync_bitable.py      同步到飞书多维表格（按 episode_id 去重，可重复跑）
  charts_radar.py      Apple 榜单雷达（已降级为可选，见「已知限制」）

prompts/
  daily_digest_prompt.md  ★ 判断规格：听众画像、语体、评分标准、输出 schema

tests/
  test_pipeline.py     56 项，全部用合成 fixture，不依赖 data/

scripts/
  install_schedule.sh  launchd 定时任务

data/
  shows_seed.txt       订阅列表（一行一档，可直接编辑）
  watchlist_seed.txt   盲区 watchlist
  feeds.json           解析结果
  digest_pack.json     给模型的证据包
  judgments.json       模型的判断
newsletters/           产出的 md 和 html
cache/transcripts/     正文缓存（按 episode id）
```

---

## 六、给一份 podcast list

一行一个节目名就够，不用管格式：

```bash
cat > data/shows_seed.txt <<'EOF'
Acquired
Invest Like the Best
Odd Lots
Capital Allocators
EOF

python3 pipeline/resolve_feeds.py data/shows_seed.txt data/feeds.json
python3 pipeline/resolve_youtube.py
python3 pipeline/shows_table.py      # ← 核对这一步不要跳过
```

想更精确可以用 `显示名|作者|搜索关键词` 三段式，帮助消歧。

### 为什么必须看核对表

节目名 → RSS 是靠 iTunes 搜索猜的，**会猜错**。同名节目、改过名的节目、
被播客托管商克隆的马甲 feed，都可能拿到高分。`shows_table.py` 把
「你给的名字」和「实际订到的 feed」并排放，匹配分 <80 的行标 ⚠️ 排在最前：

| 你给的名字 | 实际订到 | 作者 | 匹配分 | 需核对 |
|---|---|---|---|---|
| AI Ascent | ASCENT | Fangdi Pan & Linda Zhang | 40.0 | ⚠️ |
| Acquired | Acquired | Ben Gilbert and David Rosenthal | 140 | |

上面第一行是真实案例：`AI Ascent`（Sequoia）被错配到一档完全无关的 `ASCENT`。
**张冠李戴比抓不到更糟**——你会拿到一份煞有介事、但讲的根本不是那档节目的判断。

`--csv` / `--out` 可以导出给别人看。

---

## 七、投递渠道

至少配一个，**没配的渠道自动跳过，不算失败**。凭据全部走 `.env`（见 `.env.example`）。

| 渠道 | 配什么 | 说明 |
|---|---|---|
| **邮件**（主） | `./scripts/setup_email.sh` | HTML + 纯文本双份。交互式配置，密码不经过命令历史 |
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | 自动分页（4096 上限），附 md 全文 |
| 飞书群 | `LARK_WEBHOOK` | 交互卡片 |
| 飞书私聊 | `LARK_USER_ID` | 摘要 + md 附件，走 lark-cli |

**渠道之间失败隔离。** 推送是转发层，没有存储价值——判断已经落在
`data/judgments.json` 和多维表格里。所以 Telegram 挂了不该让邮件发不出去，
更不该让整条流水线的退出码变成失败、进而让定时任务重跑一遍
（重跑意味着重新转录、重新调模型，还会把已成功的渠道发第二遍）。

```bash
./scripts/setup_email.sh                   # 交互式配好邮件并发一封测试
python3 pipeline/deliver.py --dry-run      # 检查配置和渲染，不实发
python3 pipeline/deliver.py --only email   # 只发邮件
```

**Gmail / Outlook 必须用应用专用密码，不是登录密码。** Gmail 从 2022 年 5 月起
不再接受账号密码做 SMTP 认证，用登录密码会固定返回
`535 Username and Password not accepted`。生成入口：
[两步验证](https://myaccount.google.com/signinoptions/twosv) ->
[应用专用密码](https://myaccount.google.com/apppasswords)。

---

## 八、跑起来

### 初始化（只需一次）

```bash
brew install ffmpeg
uv tool install yt-dlp
uv venv .venv --python 3.12 && .venv/bin/python -m pip install parakeet-mlx
cp .env.example .env        # 填入凭据
python3 pipeline/resolve_feeds.py data/shows_seed.txt data/feeds.json
python3 pipeline/resolve_feeds.py data/watchlist_seed.txt data/watchlist_feeds.json
python3 pipeline/resolve_youtube.py
```

### 日常

```bash
./run_daily.sh                 # 完整跑通并投递
./run_daily.sh --days 3        # 改回看窗口（默认 1.5 天）
./run_daily.sh --no-asr        # 跳过本地转录（快，但覆盖率降到 ~65%）
./run_daily.sh --dry           # 干跑：不写 seen 状态、不实际发送
./run_daily.sh --stop-at-pack  # 只跑确定性部分，停在 digest_pack.json
```

退出码：`0` 全部成功 / `1` 有步骤失败 / `2` 参数错误。

### 判断环节的后端

`judge.py` 默认 `auto`：有 `ANTHROPIC_API_KEY` 走 API，否则走本机 `claude` CLI
（复用 Claude Code 订阅授权，不需要额外的 key）。

```bash
python3 pipeline/judge.py --dry-run          # 只看 prompt 规模和后端选择
python3 pipeline/judge.py --backend api      # 强制走 API
```

走 CLI 时如果报 `OAuth session expired`，在终端跑一次 `claude` 重新登录即可。

### 定时跑

```bash
./scripts/install_schedule.sh          # 每天 07:00
./scripts/install_schedule.sh 6 30     # 每天 06:30
./scripts/install_schedule.sh --uninstall
```

用 launchd 而不是 cron：机器在预定时间睡着时 cron 那一次直接丢掉，
launchd 会在唤醒后补跑。笔记本场景这条很关键。

### 测试

```bash
python3 -m unittest discover -s tests -v    # 56 项，零依赖
```

全部用合成 fixture，不读 `data/` 下的真实数据——那些文件是 gitignored，CI 上不存在。

---

## 九、多维表格归档

判断结果会累积到飞书多维表格，含判定、评分、主题标签，以及抓取侧的元数据
（正文来源、可信度、原始/压缩词数、广告切除量）——后者是为了以后能回答
「哪档节目老是拿不到全文」这类问题。

按 `episode_id` 去重，重复跑只会更新不会新增；发现历史重复行会自动清理。
首次初始化用 `python3 pipeline/sync_bitable.py --init`——没配 `LARK_BASE_TOKEN`
时会自动新建一个 Base 并把 token 写回 `data/bitable.json`。

---

## 十、凭据安全

**这个仓库是公开的。** 所有凭据走 `.env`（已在 `.gitignore` 里）：

- SMTP 密码泄漏 = 别人能以你的名义发邮件
- 飞书群 webhook URL 泄漏 = 任何人能往你的群里发消息
- Telegram bot token 泄漏 = 别人能控制你的 bot
- Base token 泄漏 = 表格读写入口公开

`.gitignore` 同时排除了 `cache/`、`data/*.json` 的运行时产物和 transcript 正文——
那些是第三方播客内容，不进仓库。

---

## 十一、已知限制（不粉饰）

1. **AI Ascent 拿不到**。Spotify 独占，Apple 无收录，无公开 RSS。付费源也无解。
2. **Stratechery 用公开替代**。播客正片是 Passport 订阅者私有 RSS；已换成
   Ben Thompson 的公开节目 Exponent + Sharp Tech（观点来源相同，不是同一档节目）。
   注：Exponent 最近一集是 2022 年，实际已停更。
3. **YouTube 会瞬时限流**。搜索偶尔返回空结果，已加重试；但这也是为什么本地 ASR 必须是主干而非备选。
4. **榜单发现（`charts_radar.py`）没有实用价值**。实测 Apple Business/Technology 榜前列是
   Crime Junkie、Habits and Hustle、Coffeez with Joe Shalaby 这类内容——榜单优化的是大众流行度，
   不是"不听会后悔"。盲区覆盖靠 `watchlist_seed.txt` 策展，脚本保留但不进日常流程。
5. **Bloomberg Surveillance 一天发 3 条**，信噪比低。已在流水线里限制 watchlist 单档每天最多 2 集，
   如果还嫌吵就从 watchlist 里删掉。
6. **`notes_only` 的集只能给判断，不给细节**。这类集在 newsletter 里会明确标注
   「no transcript — call made on the description only」，校验层强制 `key_points` 为空。
7. **判断质量依赖模型**。默认 `claude-opus-5`；换成 `claude-sonnet-5` 能省钱，
   但筛选的区分度是这个产品的全部价值，降级前先看几天产出再决定
   （`PODCAST_JUDGE_MODEL` 环境变量）。
