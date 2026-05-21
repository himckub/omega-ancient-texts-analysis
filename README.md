# Omega Ancient Texts Analysis

> 用 Omega 形式数学翻译古典经典 — 每一卦/章对应一个 Lean 4 定理、一支多媒体内容、一个可发布素材单元

> **Broadcast role**: 古典文献内容源与发布看板。本仓库沉淀文本、结构映射、生成稿、媒体素材和 release 产物；自动化发布能力由 [`broadcast-kit`](https://github.com/ChronoAIProject/broadcast-kit) 与本地运营 harness 承接。

## 这个项目在做什么

把 [automath](https://github.com/the-omega-institute/automath) 自动发现的形式化数学结果（10K+ Lean 4 定理）逐条对应到《易经》《道德经》《黄帝内经》《孙子兵法》《几何原本》《庄子》六部经典上，然后通过 NotebookLM、SlideSync 和新的可控视频生成路径产出双语视频、slides、音频与封面，并接入自动化发布流程。

**核心主张**: 我们区分"形式映射"和"隐喻类比"。每个映射都引用具体的 Lean 4 定理名，可追溯到 automath 仓库里的证明。

## Omega 宣发看板

本仓库是 Omega 古典文献内容源，也是古典文献宣发的看板入口。学术宣发和古典文献宣发共用一套内容生成、发布、反馈和复盘方法，但账号定位分开：

- **宇宙笔记**：古典文献内容，当前主线是《易经》六十四卦、道德经、黄帝内经、孙子兵法、几何原本、庄子。
- **宇宙回声**：学术内容，当前主线是 Omega 论文、数学结构、研究进展和方法论复盘。

### 日更节奏

| 时间（+08:00） | 内容线 | 当前用途 | 状态 |
|---|---|---|---|
| 12:30 | 短视频测试 | HyperFrames + TTS + SlideSync 的新格式 A/B 测试 | 设计中 |
| 20:00 | 长视频稳定档 | 已生成的长视频每日定时发布 | 运行中 |

当前目标是每天保持两条稳定产出：20:00 承接已经跑通的长视频库存，12:30 用于迭代短视频生成、封面、文案和平台反馈闭环。

### 平台与账号

| 平台 | 古典文献账号 | 学术账号 |
|---|---|---|
| 抖音 | 宇宙笔记 | 宇宙回声 |
| 小红书 | 宇宙笔记 | 宇宙回声 |
| YouTube | - | Holonomy Universe / Holonomy Echo |
| TikTok | - | Holonomy Universe |

### 工作区入口

| 目录 | 责任边界 |
|---|---|
| `../omega-broadcast-local` | Omega 本地运营 harness：账号登录态、发布队列、证据、反馈、实验记录。 |
| `../broadcast-kit` | 通用自动宣发工具包：任何同事或 agent 登录一次后可复用的发布能力。 |
| `../omega-ancient-texts-analysis` | 古典文献内容源：传统文本、生成稿、古籍系列素材。 |
| `../Omega-paper-series` | 学术内容源和展示站点：论文、研究说明、学术视频和发布产物。 |

需要做宣发的同事或 agent，应先读 `../broadcast-kit` 的通用文档；如果是在 Omega 账号体系里操作，再读 `../omega-broadcast-local` 的本地 harness 说明，沿用已有的发布证据和防重发规则。

### 三组示例对照

| 数学对象（Lean 4） | 古典对应 | 一句话理解 |
|---|---|---|
| `fibonacci_cardinality` :: `card (X m) = fib (m+2)` | 易经 21 GMS-valid 卦 / "知止" | 长度 m 的 No11 二进制串数等于 `fib (m+2)`；21 个 GMS-valid 卦（满足 No11 / 黄金均移位约束的卦）不是随意子集，是 `X_6` 的全部居民 |
| `fold_is_idempotent` :: `Fold (Fold w).1 = Fold w` | 道德经"将欲弱之必固强之" | Fold 两次 = Fold 一次；系统修正不是直线回拉，是经过一次过量显露的折返 |
| `zeckendorf_uniqueness` :: `zeckIndices x = zeckIndices y → x = y` | 道德经"见素抱朴" / 几何原本 Book X | 稳定对象由其 Zeckendorf 稀疏分解唯一决定 — 这是"朴"的形式刻画 |

完整 10 篇跨文本综合：`workspace/synthesis/synthesis_NN_*.md`。

## 内容分层架构

```
Level 0  Omega 研究院首页 (https://the-omega-institute.github.io/Omega-paper-series/)
         │
Level 1  Master 旗舰视频 (7 支，每支覆盖一部完整经典)
         │   易经 · 道德经 · 黄帝内经 · 孙子兵法 · 几何原本 · 庄子 · 论文总览
         │
Level 2  Synthesis 跨文本综合 (10 支，每支追踪 1 个 Omega 定理跨 6 部经典 + Gen 2 论文)
         │
Level 3  Category 类别视频 (66 篇 cultural essay × 视频)
         │   每部经典拆成 8–12 个主题类别
         │
Level 4  Per-Unit 逐卦/逐章 (易经 64 卦 + 道德经 81 章 = 145 篇)
```

**宣发策略**: Master 首发 → Synthesis 次发 → Category 日常 → Per-Unit 稳定日更。
自动化发布的通用能力由 [`broadcast-kit`](https://github.com/ChronoAIProject/broadcast-kit) 承接；Omega 账号体系的本地队列、证据和反馈闭环在本地运营 harness 中维护。

## 产出在哪里

```
workspace/
├── 道德经/ 易经/ 黄帝内经/ 孙子兵法/ 几何原本/ 庄子/   分类清单 + category essay
├── 易经/hexagrams/all/                                   64 卦 dossier
├── synthesis/                                            10 篇跨文本综合 essay
├── artifacts/                                            本地视频/slides/audio/封面缓存
│   ├── categories/<book>/<slug>/                         category 产出
│   ├── 易经/hexagram-NN-pinyin/                          逐卦产出
│   ├── 道德经/daodejing_chapter-NN/                      逐章产出
│   ├── masters/                                          7 支旗舰
│   ├── synthesis/                                        10 支综合
│   └── releases/<release-tag>/                           release 打包暂存区
├── publish_registry.json                                 自动化发布与媒体索引接口
texts/                                                    古典原文语料库
tools/                                                    自动化脚本（31 个，按 8 类组织，见下）
```

**展示网站** → [Omega-paper-series](https://github.com/the-omega-institute/Omega-paper-series)（双语 i18n、视频嵌入）
**视频/Slides** → 三个 GitHub Release：

| Release | 内容 |
|---|---|
| `cultural-media-v1` | 文化内容视频 / slides / infographic |
| `papers-media-v1`   | 9 篇 Gen 2 论文视频 / slides |
| `master-videos-v1`  | 7 支旗舰中文 master |

**视频直链格式:**
```
https://github.com/the-omega-institute/Omega-paper-series/releases/download/{release-tag}/{filename}
```

## 当前进度

来源：`workspace/publish_registry.json`（扫 `workspace/artifacts/` 实时生成）。

| 古典著作 | Category | 逐章/卦 | Master | Synthesis | 总条目 | Ready |
|---|---:|---:|:---:|---:|---:|---:|
| 道德经 | 12 | 81 | ✓ | — | 94 | 18 |
| 易经 | 12 | 64 | ✓ | — | 77 | 75 |
| 黄帝内经 | 12 | — | ✓ | — | 13 | 4 |
| 孙子兵法 | 10 | — | ✓ | — | 11 | 7 |
| 几何原本（en） | 8 | — | ✓ | — | 10 | 9 |
| 庄子 | 12 | — | ✓ | — | 13 | 13 |
| 跨文本综合 | — | — | — | 10 | 10 | 9 |
| 论文总览 | — | — | ✓ | — | 1 | 1 |
| **合计** | **66** | **145** | **7** | **10** | **229** | **136** |

进行中：易经全套音频用 `md+slides.pdf` 重生为中文（早期音频部分为英文），见 `tools/yijing_audio_md_plus_slides_release.py`。

### 基础设施

- **MemPalace MCP**: 26,524 drawers 横跨 14 个 room（`omega` 10K · `automath` 9.5K · `chrono_ai_ceo` 454 · 等），语义搜索 + KG 待填充
- **Lean 4 anchors**: 190 篇 cultural 文章带定理级锚点
- **Theorem reverse index**: 16 个 top 定理 → 引用文章的反向索引（数学侧导航）

## 管线架构

```
古典原文 (texts/)
    │
    ▼  Claude 分类 → workspace/<book>/classification.json
    ▼  Codex 撰写 → workspace/<book>/generated/*.md（含定理锚点）
    ▼  Claude 审核（无 backflow / 定理锚点准确）
    │
NotebookLM 多媒体生成
    │   tools/notebooklm_batch.py        单次批量
    │   tools/notebooklm_parallel.py     火后即忘并发 + --recover 恢复下载
    │   tools/build_master_notebooks.py  master 旗舰
    │   tools/build_mini_masters.py      mini-master 类别
    │
    ▼  artifact 下沉 (sync_artifacts.sh / restructure_artifacts.py)
    │
SlideSync 视频合成（可选 / 字幕开关）
    │   tools/slidesync_release_batch.py
    │   tools/yijing_audio_md_plus_slides_release.py  ← md+slides 一起喂 NotebookLM 重生中文音频
    │
    ▼  build_publish_registry.py / build_covers.py / validate_media.py
    │
Release 智能路由
    │   tools/upload_to_github_release.py
    │   → cultural-media-v1 / papers-media-v1 / master-videos-v1
    │
    ▼  publish_registry.json（统一接口）
    │
自动化发布流程 → 社交平台分发与反馈采集
```

## 工具列表（31 Python 脚本 + 2 shell，按 8 类）

### Discovery
| 脚本 | 功能 |
|---|---|
| `build_omega_theorem_index.py` | `analysis.omega_bridge` + `theorem_mapper` 给每部经典生成 theorem 候选索引 |
| `build_theorem_index.py` | 用 MemPalace 反搜：每个 top 定理 → 引用它的 cultural 文章索引 |
| `inject_theorem_anchors.py` | `## Omega 定理锚点` 段落自动注入 cultural essay |

### Source-fetch
| 脚本 | 功能 |
|---|---|
| `fetch_yijing_source_texts.py` | 抓易经 64 卦原文（Wikisource，按 King Wen 表） |
| `fetch_daodejing_source_texts.py` | 抓道德经原文，按章号切分 |
| `generate_yijing_hexagram_dossiers.py` | 64 卦结构 dossier 生成（含定理注解） |
| `generate_daodejing_chapter_pages.py` | 81 章映射页生成（含定理注解） |

### NotebookLM
| 脚本 | 功能 |
|---|---|
| `notebooklm_local.py` | 单文件入口：md → infographic / slides / audio / video |
| `notebooklm_batch.py` | 批量版，支持 zh / en 语言 profile |
| `notebooklm_parallel.py` | 火后即忘并发触发 + `--recover` 异步下载 |
| `build_master_notebooks.py` | 每部经典 1 个 master notebook |
| `build_mini_masters.py` | 每个 category 1 个 mini-master notebook |
| `bilingual_generation.py` | zh + en 双语 artifact 触发 + 下载 |
| `regenerate_chinese.py` | 清掉 FAILED + 用 `language="zh"` 重跑 |
| `regenerate_paper_infographics.py` | 9 篇 paper infographic 重建（synthesis brief 模板） |

### Slides + Video
| 脚本 | 功能 |
|---|---|
| `build_slide_media_fallback.py` | NotebookLM 不稳定时的 slide PDF → PNG / MP4 兜底 |
| `batch_audio_video.py` | audio + slides PDF → ffmpeg 合成 mp4 兜底 |
| `build_covers.py` | 4:3 / 3:4 封面 PNG（适配抖音双比例） |
| `slidesync_release_batch.py` | SlideSync audio+slides → 视频 + release 打包 |
| `yijing_audio_md_plus_slides_release.py` | 易经 md+slides → NotebookLM 中文音频，可选 SlideSync |
| `yijing_chinese_release.py` | 易经 category 中文音频（同步版） |
| `yijing_chinese_release_async.py` | 同上的并发版 |

### Validation
| 脚本 | 功能 |
|---|---|
| `validate_media.py` | ffprobe 验证视频时长 / 音量 / 编码 / 分辨率，生成 `workspace/validation_report.json` |

### Registry
| 脚本 | 功能 |
|---|---|
| `build_yijing_hexagram_registry.py` | 64 卦结构 registry（King Wen + 三爻元数据） |
| `build_daodejing_chapter_registry.py` | 81 章 registry |
| `build_hexagram_manifests.py` | 64 个 hexagram artifact 目录的 `manifest.json` |
| `build_publish_registry.py` | 统一发布索引 `publish_registry.json` |
| `build_synthesis_release_pack.py` | synthesis `media_registry.json` + NotebookLM source markdown |

### Release
| 脚本 | 功能 |
|---|---|
| `upload_to_github_release.py` | 智能路由分流到 cultural / papers / master 三个 release |
| `rename_paper_assets.py` | NotebookLM 自动命名 → canonical 前缀 |

### Other
| 脚本 | 功能 |
|---|---|
| `restructure_artifacts.py` | 扁平 artifacts → 按经典分目录的层级结构 |
| `sync_artifacts.sh` | NotebookLM artifact 下载 + 自动上传到 release（可 cron 挂） |
| `mempalace_mcp.sh` | MemPalace MCP server wrapper（注册到 Claude Code） |

## 快速开始

```bash
# 1. NotebookLM venv（已存在于本机）
python3 -m venv .venv
. .venv/bin/activate
pip install notebooklm-py mempalace pyyaml

# 2. 登录（一次性，state 写到 ~/.notebooklm/storage_state.json）
notebooklm login

# 3. 查看已有 notebooks
notebooklm list

# 4. 批量生成（同步串行）
python tools/notebooklm_batch.py \
    --batch workspace/庄子/generated/ --type slides

# 5. 火后即忘并发触发（推荐 fan-out 时用）
python tools/notebooklm_parallel.py \
    --book 易经 --parallel 5
# 30–60 分钟后回来恢复下载
python tools/notebooklm_parallel.py --recover

# 6. 中文音频专项（md + slides 一起喂 NotebookLM，跳过 SlideSync）
python tools/yijing_audio_md_plus_slides_release.py \
    --skip-video --pause-between 5

# 7. 同步 + 上传到 release
bash tools/sync_artifacts.sh --once

# 8. 持续轮询（每 60s）
bash tools/sync_artifacts.sh
```

## 关联项目

| 项目 | 说明 |
|---|---|
| [automath](https://github.com/the-omega-institute/automath) | Omega 数学发现引擎 — 10K+ Lean 4 定理 |
| [Omega-paper-series](https://github.com/the-omega-institute/Omega-paper-series) | 统一展示站点 — 论文 + 文化解读 + 视频 |
| [broadcast-kit](https://github.com/ChronoAIProject/broadcast-kit) | 通用自动化发布工具包 — 登录一次后复用平台发布、指标采集和反馈整理能力 |
| [SlideSync](../SlideSync) | audio + slides PDF → 视频合成（本仓库的下游工具） |

## License

MIT
