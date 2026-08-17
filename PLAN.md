# Carrel — 个人文献研读间（MVP 方案）

> 名字取自 library carrel（图书馆中的单人研读间）：一个只属于你、安静读文献的地方。

> 单机单用户的个人文献管理工具。自动抓取 → 解析 → 摘要 → 检索/阅读。
> 不做账号、不做趋势预测、不做 idea 生成、不做研究组/作者分析、不做会议 proceedings。

---

## 1. 目标与非目标

### 目标（MVP 要做的）
- 每天自动抓取订阅源的新论文（关键词、作者、顶刊、arXiv 分类）。
- 元数据脊梁用 **OpenAlex**，arXiv 走原生 API 保证时效性。
- 有 OA PDF 的自动下载，交给 **MinerU（Docker）** 转成 Markdown（含公式/表格/图片）。
- 每篇自动生成 **英文 + 中文摘要 + 一句话 TL;DR**。
- 全文向量检索 + 关键词检索（pgvector 混合）。
- 首页：今日新增的 TL;DR 卡片流。
- 阅读：Markdown 为主，PDF 原件留存可打开。

### 非目标（明确不做 / 以后再说）
- 账号 / 多租户 / 权限。
- 会议 proceedings 爬虫（NeurIPS/ICML/CVPR 等）。
- 付费墙 PDF 自动下载（Nature/Cell/Science 只存题录+摘要，有 OA 版才下）。
- AI 生成 idea、未来趋势预测、研究组/个人分析。
- 推荐引擎（d 模式降级为"基于库内 embedding 的语义相似度匹配"，二期再做）。

---

## 2. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | **Python 3.11 + FastAPI** | MinerU / AI 生态顺，异步支持好 |
| 数据库 | **PostgreSQL 16 + pgvector**（Docker） | 1 万篇以下单机足够，元数据+向量一库搞定 |
| 前端 | **React + Vite + TypeScript + Tailwind + shadcn/ui** | 卡片流/Markdown 组件成熟 |
| PDF 解析 | **MinerU**（Docker，HTTP API 模式） | 公式/表格/双栏处理能力强 |
| 元数据 | **OpenAlex API** | 免费无需 key，ID 体系全，跨学科 |
| 预印本 | **arXiv API**（元数据+PDF） | 补 OpenAlex 的索引延迟 |
| 生物医学 | **OpenAlex 已覆盖 PubMed/bioRxiv** | 不单独接 |
| LLM 生成 | **DeepSeek（默认）+ 火山 Ark Doubao（备用）** | 不本地部署 |
| Embedding | **火山 Ark `doubao-embedding-large`** | DeepSeek 无 embedding，统一走 Ark |
| 任务调度 | **APScheduler**（进程内） | 单用户无需 Celery/RabbitMQ |
| 存储 | 本机固定目录，**路径配置化**，二期迁 NAS | 不把文件塞数据库 |
| 容器 | **docker-compose**：Postgres + MinerU +（后期可加后端） | 一键起依赖 |

---

## 3. 源与抓取策略（混合方案）

### 订阅配置（用户自己填的 YAML / JSON）
```yaml
subscriptions:
  keywords:
    - { q: "retrieval augmented generation", sources: [arxiv, openalex] }
    - { q: "single cell sequencing", sources: [openalex] }
  authors:
    - { name: "Yann LeCun", openalex_id: "A5013214678" }
  venues:
    - "Nature"
    - "Cell"
    - "Science"
  arxiv_categories:
    - "cs.CL"
    - "q-bio.GN"
```

### 每日抓取流程
1. **arXiv 类**（关键词命中 arXiv、arXiv 分类）：直接打 arXiv API，取过去 24h，拿到 arXiv ID。
2. **其他所有订阅**（作者、Nature/Cell/Science、bioRxiv、PubMed、非 arXiv 关键词）：走 OpenAlex filter API（`from_publication_date`、`author.id`、`primary_location.source.id` 等）。
3. **归一化**：每条记录优先用 **OpenAlex Work ID** 做主键；只有 arXiv 来的，先反查 OpenAlex 拿 Work ID；拿不到就用 arXiv ID 兜底。
4. **去重**：Work ID 已在库 → 跳过。
5. **入库为 `pending`**（只有元数据+摘要，不阻塞）。
6. **PDF 优先级**：OpenAlex `best_oa_location`（正式发表版）→ 没有则 arXiv PDF → 都没有则只留题录（Nature/Cell/Science 常见）。

### 顶刊速览
Nature / Cell / Science 长期作为固定信息流：每天拉最新题录，有 OA PDF 就下载转换，没有就只展示摘要 + 原文链接。

---

## 4. 处理流水线（异步）

```
[每日抓取]
   ↓
pending（元数据入库）
   ↓
[worker: 下载 PDF]
   ↓
pdf_ready（PDF 存到 data/papers/<work_id>/paper.pdf）
   ↓
[worker: 调 MinerU]
   ↓
parsed（生成 paper.md + images/，记录解析状态）
   ↓
[worker: LLM 摘要 + 切块 + embedding]
   ↓
ready（可检索、可阅读）
```

状态机：`pending → pdf_ready → parsed → summarized/ready`；任意步骤失败 → `failed_X` 并记录错误，不阻塞队列。

### LLM 摘要
对每篇生成：
- 英文 TL;DR（一句话）
- 中文 TL;DR（一句话）
- 中文摘要（3–5 句，方法/贡献/结论）
- 关键词（5–8 个）

### 切块与向量化
- 按 MinerU 输出的 Markdown 标题层级切块，单块 ~800–1200 token，保留重叠。
- 每块调 Ark embedding，存 pgvector（HNSW 索引）。
- 检索用 **向量 + 关键词（Postgres full-text / simple）混合**，RRF 融合。

---

## 5. 数据模型（核心表）

```
papers
  id                    主键（OpenAlex Work ID 优先，arXiv ID 兜底）
  title
  authors_json          [{name, openalex_author_id, affiliation}]
  abstract              原始摘要（OpenAlex inverted index 还原）
  publication_date
  venue                 期刊/会议名（冗余，方便展示）
  doi, arxiv_id
  pdf_url, pdf_path
  md_path
  oa_status             oa / closed / none
  source                arxiv / openalex / both
  status                pending / pdf_ready / parsed / ready / failed
  tldr_en, tldr_zh, summary_zh
  keywords_json
  raw_meta_json         OpenAlex 原始 JSON（留底）
  created_at, updated_at

chunks                  全文切块
  id, paper_id
  chunk_index
  content_md
  embedding             vector(2048)（随 Ark embedding 维度）
  token_count

subscriptions           订阅配置（单用户，就一行配置或几行记录）

fetch_log               每次同步记录（时间、源、新增数、失败数）

tags / user_notes       二期：本地收藏、标签、笔记
```

---

## 6. 存储布局（可迁移 NAS）

```
data/
  papers/
    W<openalex_id>/
      paper.pdf
      paper.md
      images/
        fig_001.png
        ...
      meta.json
  attachments/          # 手动导入的文件
  config.yaml           # 订阅 + API key + 路径
```
路径全部从 `config.yaml` 读，迁 NAS 只改一个 `storage.root`。

---

## 7. 页面

1. **首页（今日新增）**：TL;DR 卡片流（标题 / 来源 / 作者 / 中英 TL;DR / 状态徽标 / 可点进详情）。
2. **库页**：全部论文，按时间/来源/状态筛选 + 全文搜索。
3. **论文详情页**：
   - 顶部元数据 + TL;DR + 中文摘要 + 关键词
   - Markdown 阅读视图（react-markdown + remark-math + rehype-katex，图片走后端静态服务）
   - "打开 PDF 原件"按钮
4. **订阅设置页**：关键词 / 作者 / 期刊 / arXiv 分类的增删改。
5. **同步状态页**：最近同步日志、失败列表、手动"立即同步"按钮。
6. **搜索结果页**：混合检索结果列表 + 高亮。

---

## 8. 里程碑（建议顺序，每步可验证）

- **M1 骨架**：docker-compose 起 Postgres+pgvector；FastAPI 空壳；React 空壳；配置加载（API key、路径）。
- **M2 元数据抓取**：OpenAlex 客户端 + arXiv 客户端 + 去重 + `papers` 表 + 手动同步按钮 + 库页能看到列表。
- **M3 PDF + MinerU**：PDF 下载（OA 优先）+ MinerU Docker 接入 + Markdown 落盘 + 状态机 + 论文详情能看 MD。
- **M4 LLM 摘要**：用 litellm 统一接 DeepSeek（默认）+ 火山 Ark（备用），生成摘要/TL;DR + 首页卡片流。
- **M5 检索**：切块 + Ark embedding + pgvector + 混合搜索 + 搜索结果页。
- **M6 定时同步 + 订阅 UI**：APScheduler 每日跑 + 订阅设置页 + 同步日志页。
- **M7 打磨**：失败重试、手动导入 PDF、笔记/收藏（可选）。

---

## 9. 关键风险

1. **LLM 成本**：每篇摘要 ~2k token，embedding 按切块走；1 万篇估算 DeepSeek 约几美元，Ark embedding 另计。控制：只对最终入库的论文做摘要。
2. **MinerU 性能**：CPU 慢（分钟级/篇），GPU 快。单用户每天几十篇可接受；失败要可重试。
3. **OpenAlex 速率限制**：礼貌池（带 `mailto` 参数）足够，单用户无压力。
4. **arXiv 作者消歧**：arXiv 作者名脏，所以 arXiv 结果统一反查 OpenAlex 拿规范 Author ID；反查不到才用原始名。
5. **Nature 等无 PDF**：UI 必须明确区分"可读全文"和"仅题录/摘要"，别让用户以为坏了。
6. **Embedding 维度变更**：维度写在配置里，换模型要重建索引（数据量小，无所谓）。

---

## 10. 参考项目（已调研）

> 源码已浅克隆到 `.references/` 仅供阅读，不打包进 Carrel。
> 详细的"哪些文件借鉴什么"见 [`docs/architecture.md`](docs/architecture.md)。

### 主要参考

| 项目 | 许可 | 在 Carrel 中的角色 | 复用方式 |
|---|---|---|---|
| [galleonli/paper-agent](https://github.com/galleonli/paper-agent) | MIT | **骨架蓝本**：自托管单用户 arXiv 日报，含抓取/去重/摘要/digest | 移植 `sources/arxiv.py` 的抓取与 429 退避；借鉴 `core/state.py` 的幂等思路、`pipeline.py` 的编排顺序与日志、`core/summarize.py` 的结构化防幻觉 prompt、`config.example.yaml` 的配置组织。**不移植**它的 bandit/linucb/autotune 推荐策略和 markdown 文件存储（我们用 DB+MinerU）。 |
| [J535D165/pyalex](https://github.com/J535D165/pyalex) | MIT | OpenAlex 元数据脊梁 | **直接当依赖** `pip install pyalex`，不自己写 HTTP。我们只包一层 `sources/openalex_client.py` 做字段归一化和 OA PDF 选取。 |
| [opendatalab/MinerU](https://github.com/opendatalab/MinerU) | AGPL-3.0 | PDF→Markdown（含公式/表格/图片） | **独立 Docker 服务，HTTP 调用**，不 `import` 其代码，AGPL 不传染。 |
| [Future-House/paper-qa](https://github.com/Future-House/paper-qa) | MIT | 科学文献 RAG 的切块/检索/引用验证参考 | 二期做"和论文对话"时读其 chunking 和 hybrid 检索思路；MVP 不依赖。 |
| [karpathy/arxiv-sanity-preserver](https://github.com/karpathy/arxiv-sanity-preserver) | MIT | 卡片流 UI 灵感 | 已停更，仅参考首页论文卡片/相似度排序的交互，不抄代码。 |
| [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily) | 见仓库 | 每日推荐的评分 prompt 参考 | 读它的相关性评分逻辑和 prompt 写法。 |

### 工具与生态（直接作为依赖/服务使用）

- [litellm](https://github.com/BerriAI/litellm)：统一接 DeepSeek + 火山 Ark，换模型不改业务代码。
- [FastAPI](https://fastapi.tiangolo.com/) / [SQLModel](https://sqlmodel.tiangolo.com/) / [Alembic](https://alembic.sqlalchemy.org/)
- [PostgreSQL + pgvector](https://github.com/pgvector/pgvector)
- [APScheduler](https://apscheduler.readthedocs.io/)：进程内定时同步
- React + Vite + TypeScript + Tailwind + shadcn/ui + react-markdown (+remark-math/rehype-katex)

### 仅了解架构、不抄代码（许可原因）

| 项目 | 许可 | 原因 |
|---|---|---|
| [khoj-ai/khoj](https://github.com/khoj-ai/khoj) | AGPL-3.0 | 自托管 pgvector RAG 架构参考，二期做聊天功能时对照其 ingestion/调度设计；**不拷贝代码**。 |
| [VikParuchuri/marker](https://github.com/VikParuchuri/marker) | Apache-2.0（代码）/ 模型权重自定义 | MinerU 的备选 PDF→MD 方案；如果 MinerU 在本机太重或质量不满意再评估。 |

### 索引/清单

- [handsome-rich/Awesome-Auto-Research-Tools](https://github.com/handsome-rich/Awesome-Auto-Research-Tools)：自动化科研工具全景清单，用于后续找参考。
