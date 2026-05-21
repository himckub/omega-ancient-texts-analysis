# Distribution — Ancient-Texts Broadcast Sink

> This directory is the **ancient/traditional-text end** of the 4-piece Omega broadcast pipeline. Outputs produced by the local broadcast harness (`../../../omega-broadcast-local/`) for classical-text content (易经, 道德经, 黄帝内经, 孙子兵法, 几何原本, 庄子, and their hexagram/chapter series) are exported into this repo for durable archival.

## What lives here

Distribution + broadcast outputs for ancient/traditional-text content:

- final published videos for hexagram series and chapter series
- ancient-text explainer captions and caption variants
- short-video covers (4:3 横版 / 3:4 竖版)
- classical-text audio — NotebookLM podcasts and narration tracks
- per-series performance summaries (e.g. 易经 64-卦 cohort, 道德经 81-章 cohort)
- distribution post-mortems indexed by 卦象 / 章节
- ancient-text A/B-test conclusions (caption framings, hook variants, cover variants)
- per-account summary rollups when a campaign closes

This is the **content-sink** half of the broadcast loop: durable, reviewable, agent-friendly. Live operating state stays upstream in `omega-broadcast-local`.

## The 4-piece architecture

```text
broadcast-kit            ← generic reusable publishing toolkit (platform adapters, schemas, skills)
       │
       ▼
omega-broadcast-local    ← local harness: auth, queue state, evidence, raw metrics, experiments
       │  (exports durable artifacts outward)
       ├──────────────► Omega-paper-series          (academic content sink)
       └──────────────► omega-ancient-texts-analysis (THIS REPO — ancient/traditional-text sink)
```

One line each:

- `broadcast-kit` — generic, reusable publishing capability. No Omega-specific content.
- `omega-broadcast-local` — local-only operating layer. Produces the artifacts that land here.
- `Omega-paper-series` — academic content sink. Gen 2 papers, paper retrospectives.
- `omega-ancient-texts-analysis` (this repo) — ancient-texts content sink. 易经 / 道德经 / hexagram series / classical-text podcasts and their post-mortems.

Upstream producer for everything in `workspace/broadcast_exports/` is `../omega-broadcast-local/`.

## What gets exported here from broadcast-local

When a broadcast cycle for ancient-text content closes (judgement: success, queue verified, metrics ingested, retrospective written), the following artifacts are exported into this repo:

- 易经 / 道德经 / 黄帝内经 / 孙子兵法 / 几何原本 / 庄子 final video files (after publish confirmation)
- ancient-text explainer captions (the version that actually shipped)
- short-video covers — both 4:3 and 3:4 ratios
- classical-text audio — NotebookLM podcasts used as the audio rail for explainer videos
- caption variants tested across platforms (Douyin / XHS / X / 微博)
- hexagram-series performance summaries — per-cohort rollups across all 64 卦 (or 81 章 for 道德经)
- distribution post-mortems indexed by 卦象 / 章节 / 篇章
- ancient-text A/B-test conclusions — what hook framing won, what cover ratio converted, which caption length retained

## What does NOT belong here

- **Paper-academic content** → goes to `../Omega-paper-series/docs/distribution/`
- **Generic / reusable tool code** → goes to `../broadcast-kit/`
- **Live operating state** (auth, queue state, raw metrics, in-flight evidence, experiments, quarantine) → stays in `../omega-broadcast-local/`
- **In-flight drafts** that haven't passed the broadcast harness's success contract (`JUDGEMENT: success` + `COVER_VERIFY: True` + `QUEUE_VERIFY: True`) — do not export early

## Sibling pointers

- [`../../../broadcast-kit/CATALOG.md`](../../../broadcast-kit/CATALOG.md) — reusable publishing toolkit catalog
- [`../../../omega-broadcast-local/README.md`](../../../omega-broadcast-local/README.md) — upstream broadcast harness (local-only sibling; may not exist on every machine)
- [`../../../Omega-paper-series/docs/distribution/README.md`](../../../Omega-paper-series/docs/distribution/README.md) — peer sink for academic content

## Where the files live within this repo

The exported artifacts land under [`../../workspace/broadcast_exports/`](../../workspace/broadcast_exports/) with this layout:

```text
workspace/broadcast_exports/
  videos/             — final published videos
  audio/              — NotebookLM podcasts + narration tracks
  captions/           — shipped captions + variants
  covers/             — 4:3 and 3:4 covers
  post-mortems/       — per-卦象/章节 distribution retrospectives
  ab-tests/           — A/B-test conclusions
  hexagram-series/    — series-specific bucket for hexagram cohort tracking
  accounts-summary/   — per-account campaign rollups
```

See [`../../workspace/broadcast_exports/README.md`](../../workspace/broadcast_exports/README.md) for the file-naming convention and per-bucket expectations.
