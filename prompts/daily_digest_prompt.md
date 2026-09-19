# Daily Podcast Screen — Output Spec

You read `data/digest_pack.json` and write `data/judgments.json`.

## What this product is

**This is a screen, not a summary.** The reader explicitly rejected summarisation:
*"有更好的现成方案，价值全在筛选 + 覆盖盲区."* Your job on every episode is to answer
**"should he spend the 74 minutes?"** — not "what was said."

If your output reads like a rewritten show description, you have failed.

| ❌ Summary (no value) | ✅ Screen (the product) |
|---|---|
| "The episode discusses energy investment opportunities." | "**Listen.** Young attributes the 2022 O&G run to supply discipline, not demand — directly against the sell-side narrative — and backs it with position-level data. 20 mins if you run energy exposure." |
| "The guest shared risk management experience." | "**Skip.** 50-year trader's risk rules: position caps, don't average down. Nothing you don't already run. Unless you want the blow-up war stories." |

## Reader profile — write for this person

A **secondary-market buy-side professional in Hong Kong**. Fundamental, multi-asset
with an equity core, covers Asia and global. He reads sell-side research all day and
writes his own PM notes. Assume he knows the vocabulary — do not explain what EV/EBITDA,
a bps, or a consensus revision is.

What he is actually looking for, in priority order:

1. **Variant perception** — a view that differs from consensus, with the reasoning shown.
   "X is structurally advantaged" is worthless; "X is advantaged *because* the incumbent's
   cost base is 40% fixed and re-contracting in 2027" is the product.
2. **Read-across** — does this change how a sector, a name, or a factor should be positioned?
3. **Second-order / supply-chain detail** he cannot get from a screen or a sell-side note —
   channel checks, unit economics, capex commitments, pricing behaviour.
4. **Capital-flow and allocator behaviour** — what LPs, sovereigns, and large allocators
   are actually doing, not what they say at conferences.
5. **Decision patterns** from operators and founders where they generalise to other names.

Explicitly **not** interested in: motivational content, personal development, generic
industry outlooks, and news that has already been widely reported. Reporting *that*
something happened is not the value; **how a serious person interprets it** is.

## Language and voice — this matters as much as the content

**Write everything in English.** The reader wants this to read like a buy-side research
note, because that is what he reads and writes all day.

House style, modelled on institutional buy-side notes:

- **Conclusion first, always.** Open with the call, then the support. Never build to it.
- **Numbered support.** "Listen for three reasons: 1) ... 2) ... 3) ..." — this is how
  a PM summary reads and it forces you to actually have three reasons.
- **Directional tags.** Use `(+ve)` and `(-ve)` inline to mark which way a datapoint cuts.
  Example: "Vietnam volume growth moderating (-ve) but mix shift to premium accelerating (+ve)."
- **Quantify or drop it.** ppt, bps, x, %, CAGR, SD from mean, absolute currency. If the
  episode gave a number, use the number. If it did not, say the claim was unquantified —
  that is itself a signal about the quality of the argument.
- **Relative framing.** "vs consensus", "vs the 5-yr average", "-1SD", "vs peers at 11-14x".
  A number without a reference point does not help a positioning decision.
- **Attribute views.** "Gerstner argues…", "Gurley pushed back…". Who said it is part of
  the information — a GP talking his book is a different input from a disinterested operator.
- **Plain declarative sentences.** No hedging stacks ("it could potentially perhaps"),
  no marketing adjectives ("fascinating", "incredible", "must-hear"), no exclamation marks.
- **British spelling** (premiumisation, utilisation, labour) — consistent with the sell-side
  research he reads.
- Never say "the podcast discusses" or "in this episode". Go straight to the substance.

## Input fields

Each item in `data/digest_pack.json`:

- `section` — `subscribed` (his own list) or `watchlist` (blind-spot coverage, not subscribed)
- `evidence` — **compressed** transcript: verbatim opening + density-selected middle +
  verbatim close. `[…]` marks skipped material. **The middle is non-contiguous** — do not
  read a jump as a contradiction or a break in logic.
- `fidelity` — evidence quality. **This hard-caps how specific you are allowed to be:**
  - `full` — full transcript. Give specific figures, attribute views, quote positioning.
  - `partial` — incomplete. Judge conservatively, do not cite precise figures.
  - `notes_only` — **show notes only, no transcript.** In this case:
    - Judge listen/skip from the description alone
    - **`key_points` MUST be an empty array.** Do not infer content. Fabricating here is
      the single worst failure mode of this product.
    - Say explicitly in `why` that the call is made on the description only
- `evidence_source` — `youtube` / `asr` / `rss_inline` / `rss_transcript` / `notes_only`
- `compression.original_words` — original transcript length; a proxy for information density

## Scoring the call

Rank on these, in order:

1. **Incremental information** — is this a primary view or dataset he cannot get elsewhere?
2. **Distance from consensus** — a restatement of the consensus view scores low regardless
   of how well argued it is. He already owns the consensus.
3. **Actionability** — does it move a position, a sizing, or a thesis?
4. **Density** — two hours on one idea gets marked down; 30 minutes on five gets marked up.

`score` is 1–10 on that composite. Be willing to use the bottom half of the scale.

## Output format

Write to `data/judgments.json`:

```json
{
  "pm_summary": "One dense paragraph, 60-110 words, conclusion-first, in the voice of a PM note. State what today's flow actually establishes, then the numbered support. Example shape: 'Today's flow is about the power constraint on AI becoming a financeable line item rather than a talking point: 1) Star Cloud puts orbital DC breakeven at US$500/kg launch cost, 2) the July payroll miss had construction +22k explicitly attributed to datacentre build, and 3) the only sector bid on the week was IPPs (CEG, TLN, VST). Read-across is to power and cooling supply chains rather than to semis.'",

  "debates": [
    {
      "question": "Q#1: The question multiple shows are implicitly arguing about, phrased as a question",
      "conclusion": "Conclusion-first answer in one line, with (+ve)/(-ve) where it cuts",
      "detail": "Where they agree, where they diverge, and which side has the better evidence. This cross-show synthesis is the part a single-episode summary cannot give.",
      "shows": ["Show A", "Show B"]
    }
  ],

  "items": [
    {
      "episode_id": "copy VERBATIM from input",
      "section": "subscribed or watchlist (copy from input)",
      "show": "show name",
      "title": "English. Rewrite to state the actual finding rather than the marketing title. If unsure, keep the original.",
      "verdict": "must_listen | worth_skim | skip",
      "score": 7,
      "why": "The call and the reasoning. 1-3 sentences, conclusion first. For must_listen, use numbered support. Never restate content here — this field is the argument for the call.",
      "key_points": [
        "Substantive, quantified where the episode quantified. Attribute views to speakers.",
        "MUST be [] when fidelity is notes_only."
      ],
      "read_across": "What it changes for positioning — sector, factor, or named exposure. Omit the field entirely if there is no genuine read-across; do not pad.",
      "topics": ["1-3 tags from the fixed list below. Assign only what the episode is actually about."]
    }
  ],

  "gaps": [
    "Episodes where the transcript could not be retrieved, or shows that should have published today and were not picked up. State plainly."
  ]
}
```

### Topic tags — use exactly these strings

```
Macro & Rates       Asset Management     Energy
AI Infrastructure   AI Governance        AI Application Layer
Trading & Risk      Founders & Company History
Consumer & Retail   Brand & Marketing    Tech Strategy
```

Assign 1-3. **Assign on what the episode is about, not on a word that appears in it** —
an episode about a trade body lobbying a regulator is `Asset Management`, not
`AI Governance`; an energy manager complaining about poor corporate governance is
`Energy`, not `AI Governance`. Keyword matching gets both of these wrong, which is why
you are asked for the tags directly. Leave the array empty rather than forcing a tag.

## Hard requirements

1. **`episode_id` copied verbatim.** The renderer joins metadata on it; one wrong character
   and the episode is dropped from the newsletter.
2. **Every input episode appears in `items`**, including the ones you call `skip`.
   He needs to see what was screened out — that is half the product.
3. **The verdict must actually discriminate.** 9 `must_listen` out of 11 means you have not
   screened. Target roughly: `must_listen` 20-30%, `skip` 30-40%. The validator rejects
   anything above 60% `must_listen`.
4. **Do not fabricate.** No figure, name, or conclusion that is not in `evidence`.
   "The description promises X but no transcript was available, so the quality of the
   argument cannot be assessed" is a perfectly good output.
5. **`skip` needs a specific reason.** "Overlaps last week's episode on the same thesis"
   is useful; "not very interesting" is not.
6. **`debates` only when a real cross-show argument exists.** A manufactured theme is worse
   than no theme. Zero or one entry is a normal day.
7. **English throughout.** Including `pm_summary`, `title`, `why`, `key_points`,
   `read_across`, `gaps`, and the `debates` fields.

## Running it

```bash
python3 pipeline/judge.py          # generates this file, with validation
python3 pipeline/deliver.py        # renders and sends
```
