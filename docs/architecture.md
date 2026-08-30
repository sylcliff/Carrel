# Carrel 技术骨架与复用清单

> 本文是 Carrel 的工程落地蓝图。明确每个模块自己写什么、用什么依赖、从哪个参考项目借鉴哪段代码。
> 参考项目源码已 clone 到 `.references/`(仅用于阅读,不打包进项目)。

---

## 1. 参考项目阅读结论

### 1.1 paper-agent(galleonli/paper-agent,MIT)— 主要骨架参考
通读后的关键结论:

| 它的模块 | 文件 | 我们借鉴什么 | 我们不照搬什么 |
|---|---|---|---|
| arXiv Atom 抓取 | `paper_agent/sources/arxiv.py` | **几乎可直接复用**:429 指数退避、分页、`(query) AND (cat:...)` 组合查询、按 id 去重、Atom XML 解析 | 它只用 arXiv,我们要在外面套一层 OpenAlex 补全 |
| 幂等/去重状态 | `paper_agent/core/state.py` | `seen.json` 的 `filter_unseen`/`save_seen` 思路、`normalize_paper_id`(剥 arXiv 前缀) | 我们用数据库唯一约束替代文件,但思路保留 |
| Pipeline 编排 | `paper_agent/pipeline.py` | **编排顺序就是我们要的**:fetch → lookback 过滤 → 去重 → 生成摘要 → 写本地 → 持久化 seen。日志字段设计也值得抄 | 它的 bandit/linucb/autotune 推荐策略**全部不要**(我们砍了推荐);它写 markdown 笔记文件,我们写数据库 + 调 MinerU |
| 数据模型 | `paper_agent/core/models.py` | `Paper` dataclass 的字段取舍(id/title/summary/authors/categories/updated/link_abs/link_pdf) | 我们字段更多(OpenAlex ID、doi、venue、oa_status、状态机),用 SQLAlchemy/SQLModel |
| LLM 摘要 | `paper_agent/core/summarize.py` | **prompt 模板的结构化思路**(分节、"信息不足就明说"防幻觉)、失败不阻塞主流程、`temperature=0.2` | 它只调 OpenAI,我们用 `litellm` 统一接 DeepSeek + 火山;它只生成英文研究总结,我们要中英 TL;DR + 中文摘要;我们用 section-picker 切片的 parsed md(见 `carrel/pipeline/_section_picker.py`)作为 LLM 输入,不是头 N 字符的简单截断 |
| 配置 | `config.example.yaml` + `paper_agent/core/config.py` | YAML 单文件配置 + pydantic 校验的模式 | 我们的订阅结构更丰富(关键词/作者/期刊/arXiv 分类分开) |
| BibTeX/RIS 导出 | `paper_agent/export/bibtex_ris.py` | 二期可抄,导出逻辑 | MVP 不做 |

**重要判断**:paper-agent 没有数据库、没有全文解析、没有向量检索——它是一个"arXiv 日报生成器"。所以它的 pipeline 编排和抓取/去重可直接借鉴,但存储、解析、检索三层我们要自己建(或用别的库)。

### 1.2 pyalex(J535D165/pyalex,MIT)— 直接当依赖
- **直接 `pip install pyalex`**,不要自己写 OpenAlex HTTP 调用。
- 用法要点(来自源码):
  - `Works().filter(publication_date=">2026-08-10", author={"id": "A..."}, primary_location={"source": {"id": "S..."}}).get()`
  - `Works()["W2741809807"]` 或 `Works()["https://doi.org/..."]` 取单篇。
  - `Works().similar("text")` 语义检索(OpenAlex 自带,可用于二期 d 模式)。
  - 配置邮箱进礼貌池:`pyalex.config.email = "you@x.com"`(更快更稳);有 key 就 `pyalex.config.api_key`。
  - OpenAlex 摘要是 inverted index,pyalex 会自动还原成纯文本。
- 我们只需要在它外面包一层 `carrel/sources/openalex_client.py`,做字段归一化和 `best_oa_location` 的 PDF 选取。

### 1.3 其他仅作参考(不 clone,按需读)
- **paper-qa(Future-House, MIT)**:切块策略和混合检索思路,二期做"和论文对话"时读它的 chunking/rerank。
- **khoj(AGPL-3.0)**:pgvector 用法参考。**注意 AGPL,不抄代码,只看架构。**
- **MinerU(opendatalab, AGPL-3.0)**:作为 Docker 服务调用,不链接其代码,AGPL 不传染我们的应用(独立进程通过 HTTP 通信)。

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    前端: React + Vite + TS                   │
│  首页(今日卡片流) / 库 / 论文详情(MD阅读) / 搜索 / 订阅设置 / 同步状态  │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST / JSON
┌───────────────────────────▼─────────────────────────────────┐
│                   后端: FastAPI (carrel/)                    │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │ /sync    │   │ /papers  │   │ /search  │   │ /subs    │  │
│  │ /jobs    │   │ /papers/{id}│ │(hybrid)  │   │ (CRUD)   │  │
│  └────┬─────┘   └──────────┘   └──────────┘   └──────────┘  │
│       │                                                      │
│  ┌────▼─────────────────────────────────────────────────┐   │
│  │              pipeline(同步编排)                        │   │
│  │  fetch → normalize → dedup → download → parse →       │   │
│  │  summarize → chunk+embed         (状态机驱动)          │   │
│  └──┬─────────┬──────────┬──────────┬──────────┬─────────┘   │
│     │         │          │          │          │             │
│  ┌──▼──┐  ┌───▼───┐  ┌───▼───┐  ┌───▼───┐  ┌───▼────┐        │
│  │arxiv│  │openalex│  │ pdf   │  │mineru │  │ llm     │       │
│  │fetcher│ │client │  │download│ │client │  │(litellm)│       │
│  └─────┘  └───────┘  └───────┘  └───────┘  └────────┘        │
│     │         │                                            │   │
│  ┌──▼─────────▼────────────────────────────────────────┐    │
│  │         embeddings(火山 Ark via litellm)             │    │
│  └──────────────────────┬──────────────────────────────┘    │
│                         │                                    │
└─────────────────────────┼────────────────────────────────────┘
                          │
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
  PostgreSQL+pgvector   文件系统           MinerU Docker
   (papers/chunks/     data/papers/<id>/   (HTTP API)
    subscriptions/       paper.pdf
    jobs/...)            paper.md
                         images/
```

调度用 **APScheduler** 嵌在 FastAPI 进程内;同步任务用一张 `jobs` 表记录状态,前端轮询 `/jobs` 看进度。

---

## 3. 技术栈与关键依赖

| 用途 | 选型 | 备注 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | |
| ORM | SQLModel(SQLModel 是 SQLAlchemy+pydantic,最省样板) | |
| 数据库 | PostgreSQL 16 + pgvector | docker-compose 起 |
| 迁移 | alembic | |
| OpenAlex | **pyalex** | 直接依赖 |
| arXiv | 自己写(参照 paper-agent `sources/arxiv.py`) | arXiv Atom API |
| Semantic Scholar | 自己写(`sources/semanticscholar_client.py`) | S2 Graph API |
| Crossref | 自己写(`sources/crossref_client.py`) | Crossref REST API,polite-pool `mailto:` UA 提升速率 |
| LLM 统一层 | **litellm** | DeepSeek + 火山 Ark 一家一个配置,换模型不改业务代码 |
| Embedding | 火山 Ark `doubao-embedding-large`(经 litellm) | |
| PDF 解析 | **MinerU**(独立 Docker,HTTP 调用) | AGPL 但独立进程不传染 |
| 任务调度 | APScheduler | 进程内,单用户足够 |
| 配置 | pydantic-settings + YAML | |
| HTTP | httpx(异步) | |
| 前端 | React+Vite+TS+Tailwind+shadcn/ui | |
| Markdown 渲染 | react-markdown + remark-math + rehype-katex | 公式支持 |
| 测试 | pytest + pytest-asyncio | |

---

## 4. 目录结构

```
carrel/                          # 后端
  pyproject.toml
  carrel/
    __init__.py
    main.py                      # FastAPI app + 挂载 router + 启动 APScheduler
    config.py                    # pydantic-settings: 读 config.yaml + env
    db.py                        # engine/session
    models.py                    # SQLModel: Paper/Chunk/Subscription/Job/FetchLog
    schemas.py                   # API 请求/响应
    pipeline/
      __init__.py
      runner.py                  # 编排同步流程(借鉴 paper-agent/pipeline.py 的顺序与日志)
      state.py                   # 作业状态推进
    sources/
      __init__.py
      arxiv.py                   # arXiv Atom 抓取(从 paper-agent/sources/arxiv.py 移植改造)
      openalex_client.py         # 包 pyalex: 查作者、venue、近期 works、OA PDF
      normalize.py               # arXiv/OpenAlex → 统一 PaperRecord
      dedup.py                   # Work ID / arXiv ID / DOI 归一化
    ingest/
      pdf_download.py            # OA PDF 下载(best_oa_location 优先)
      mineru_client.py           # 调 MinerU Docker HTTP,落 paper.md + images/
    ai/
      __init__.py
      llm.py                     # litellm 封装: summarize(Paper) -> TLDR en/zh, summary_zh, keywords
      prompts.py                 # 摘要 prompt(借鉴 paper-agent 结构化 + 防幻觉)
      embeddings.py              # 火山 Ark embedding 封装
      chunking.py                # Markdown 按标题切块(800-1200 token,重叠)
    search/
      __init__.py
      hybrid.py                  # 向量 + tsvector 关键词, RRF 融合
    api/
      papers.py
      search.py
      subscriptions.py
      sync.py
      files.py                   # 静态服务 paper.md 引用的 images/
  migrations/                    # alembic
  tests/

frontend/                        # React 应用
  package.json
  src/
    pages/ (Today, Library, PaperDetail, Search, Subscriptions, SyncStatus)
    components/ (PaperCard, PaperList, MarkdownReader, ...)
    api/

data/                            # 本机存储(gitignore;路径配置化,二期迁 NAS)
  papers/
    W<work_id>/
      paper.pdf
      paper.md
      images/
      meta.json
  config.yaml
  carrel.db? (不用,DB 在 postgres)

docker-compose.yml              # postgres+pgvector + mineru
.env / .env.example             # API keys
.references/                    # clone 来的参考源码(不打包)
PLAN.md
docs/architecture.md            # 本文件
```

---

## 5. 数据模型

### papers
```
id                  VARCHAR 主键   # OpenAlex Work ID(W...);查不到才用 arxiv:2301.12345
id_kind             VARCHAR        # openalex | arxiv
title               TEXT
authors             JSONB          # [{name, openalex_author_id, affiliation}]
abstract            TEXT           # OpenAlex 还原后的纯文本 / arXiv summary
publication_date    DATE
venue               TEXT           # 冗余展示名
doi                 VARCHAR
arxiv_id            VARCHAR
pdf_url             VARCHAR
pdf_path            VARCHAR        # 相对 storage.root
md_path             VARCHAR
oa_status           VARCHAR        # oa / closed / none
source              VARCHAR        # arxiv | openalex | both
status              VARCHAR        # pending|pdf_ready|parsed|summarized|ready|failed
error               TEXT
tldr_en             TEXT
tldr_zh             TEXT
summary_zh          TEXT
keywords            JSONB
raw_meta            JSONB          # OpenAlex 原始记录留底
created_at / updated_at
```

### chunks(全文检索)
```
id BIGSERIAL PK
paper_id VARCHAR FK→papers.id
chunk_index INT
heading TEXT              # 所在标题层级
content_md TEXT
token_count INT
embedding VECTOR(2048)    # 维度随 Ark embedding 模型,写在配置里
```
索引:`USING hnsw (embedding vector_cosine_ops)`;`content_md` 建 `tsvector` 生成列做关键词检索。

### subscriptions
```
id BIGSERIAL PK
kind VARCHAR              # keyword | author | venue | arxiv_category
value TEXT                # 关键词字符串 / OpenAlex Author ID(A...) / Source ID(S...) / "cs.CL"
label TEXT                # 展示名(如 "Nature")
enabled BOOLEAN
created_at
```
唯一约束 `(kind, value)`。

### jobs(每次同步/处理任务)
```
id BIGSERIAL PK
kind VARCHAR              # sync | download | parse | summarize | embed
status VARCHAR            # queued|running|done|failed
started_at / finished_at
message TEXT
stats JSONB               # {fetched, new, downloaded, parsed, summarized, failed}
```

### fetch_log(每次同步的轻量记录,可由 jobs 替代;先保留)
用 `jobs` 表即可,不单独建。

---

## 6. 关键流程

### 6.1 每日同步(pipeline.run)
借鉴 paper-agent `pipeline.py` 的编排顺序与日志字段:
1. 读 `subscriptions`,按类型分发:
   - arxiv 分类/关键词且要求时效 → `sources/arxiv.py` 直接查 arXiv API,拿 arXiv ID 列表。
   - 作者/期刊/其他 → `sources/openalex_client.py` 用 pyalex filter `from_publication_date`(过去 24h)。
2. 对每条记录归一化成 `PaperRecord`(`normalize.py`):arXiv 来的用 arXiv ID 反查 OpenAlex 拿 Work ID 与规范作者/venue;反查不到用 `arxiv:<id>` 兜底。
3. 数据库 upsert(id 为主键),已存在且 status 不为 failed 的跳过。新增的进 `pending`。
4. 为每条 pending 入后台处理队列(进程内 BackgroundTasks / 简单 worker 循环):
   - `pdf_download`:按 `pdf_url`(OpenAlex best_oa_location → arXiv PDF)下载;无则标记 closed(停在 pending,仅题录可见)。
   - `mineru_client`:POST PDF 给 MinerU,拿回 md+图片,落盘,状态→parsed。
   - `llm.summarize`:基于 section-picker 切片的 parsed md(见 `carrel/pipeline/_section_picker.py`)生成 tldr_en/tldr_zh/summary_zh/keywords;状态→summarized。
   - `chunking` + `embeddings`:读 paper.md 切块,调 Ark embedding,写 chunks;状态→ready。
5. 更新 `jobs.stats`。

### 6.2 混合检索
- 向量:`embedding <=> :q` cosine 排序,取 top 50。
- 关键词:`plainto_tsquery('simple', q)` 查 chunks.content_md 的 tsvector,取 top 50。
- RRF 融合两路结果,聚合到 paper 级别返回。

### 6.3 配置(config.yaml)
```yaml
storage:
  root: ./data
http:
  host: 127.0.0.1
  port: 8787
openalex:
  mailto: you@example.com   # 进礼貌池
  api_key: null
llm:
  summarize_provider: deepseek       # litellm provider
  summarize_model: deepseek-chat
  embedding_provider: volcano
  embedding_model: doubao-embedding-large-text-240915  # 按 Ark 实际模型名
  fallback_provider: volcano
  fallback_model: doubao-...
mineru:
  base_url: http://localhost:8000
schedule:
  sync_cron: "0 8 * * *"   # 每天本地 8 点
```
API keys 走 `.env`(DEEPSEEK_API_KEY / VOLCANO_API_KEY),litellm 直接读环境变量。

---

## 7. 可直接复用 vs 自己写(快速决策表)

| 组件 | 决策 |
|---|---|
| OpenAlex 调用 | **用 pyalex**,别自己写 |
| arXiv 抓取 | **从 paper-agent 移植并改造**(加 24h 时间窗、按 ID 反查 OpenAlex) |
| LLM 调用 | **用 litellm**,别写两家 SDK |
| Embedding | litellm 调火山 Ark |
| PDF 解析 | **MinerU Docker**,不嵌入代码 |
| 去重/幂等思路 | 借鉴 paper-agent state.py,但用 DB 唯一约束 |
| Pipeline 编排顺序/日志 | 借鉴 paper-agent pipeline.py |
| 结构化摘要 prompt | 借鉴 paper-agent summarize.py,改成双语 + 基于 abstract |
| 数据模型/API/前端 | 自己写 |
| 切块/混合检索 | 自己写(M5);二期可参考 paper-qa |
| BibTeX 导出 | 二期抄 paper-agent export |

---

## 8. 许可注意事项
- 我们自己的代码用 **MIT**。
- **MinerU 是 AGPL-3.0**:通过 Docker 独立进程 + HTTP 调用,不把它的代码链接进我们的进程,AGPL 的 copyleft 不传染我们的应用。不要 `import mineru` 或拷贝其源码。
- **khoj 是 AGPL-3.0**:只读架构,不抄代码。
- paper-agent(MIT)、pyalex(MIT)、paper-qa(MIT):可自由借鉴/复用,保留其 license 声明即可。移植 paper-agent 代码段时在文件头注明来源。
- 论文 PDF/元数据:只处理 OA 内容;Nature/Cell/Science 拿不到 PDF 时只存题录摘要,不碰付费墙/Sci-Hub。

---

## 9. 与 PLAN.md 里程碑的对应
- **M1 骨架**:docker-compose(postgres+pgvector+mineru)、FastAPI `main.py`、config 加载、SQLModel 模型 + alembic 初始迁移、React 空壳。
- **M2 抓取**:`sources/arxiv.py`(移植)、`sources/openalex_client.py`(pyalex)、normalize、upsert、`/api/sync` + `/api/papers`,库页可见。
- **M3 PDF+MinerU**:`ingest/pdf_download.py`、`mineru_client.py`、状态机、论文详情页渲染 MD + 图片。
- **M4 LLM 摘要**:`ai/llm.py` + `ai/prompts.py`、今日卡片流。
- **M5 检索**:`ai/chunking.py`、`embeddings.py`、`search/hybrid.py`、搜索页。
- **M6 定时+订阅 UI**:APScheduler、订阅 CRUD 页、同步状态页。
- **M7 打磨**:失败重试、手动导入 PDF、笔记/收藏。
