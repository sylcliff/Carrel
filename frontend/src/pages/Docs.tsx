import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  BookOpen,
  Server,
  Code2,
  Search,
  FileText,
  MessageSquare,
  Users,
  Network,
  Tag,
  Sparkles,
  BookText,
  Terminal,
  Layers,
  Wrench,
  Activity,
  Globe,
  Zap,
  Library,
  Brain,
  FileSearch,
  Database,
  ListChecks,
  Rss,
  CheckCircle2,
  CircleDashed,
  ArrowRight,
  Cloud,
  Cpu,
  Newspaper,
  Telescope,
  Bot,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Reusable presentational bits
// ---------------------------------------------------------------------------

function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em] text-foreground">
      {children}
    </code>
  );
}

function Pill({
  tone,
  children,
}: {
  tone: "ok" | "wip";
  children: ReactNode;
}) {
  const cls =
    tone === "ok"
      ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200"
      : "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        cls,
      )}
    >
      {tone === "ok" ? (
        <CheckCircle2 className="h-3 w-3" />
      ) : (
        <CircleDashed className="h-3 w-3" />
      )}
      {children}
    </span>
  );
}

function CodeBlock({ children }: { children: string }) {
  return (
    <pre className="overflow-x-auto rounded-md bg-slate-950 px-4 py-3 text-xs leading-relaxed text-slate-50 dark:bg-slate-900">
      {children}
    </pre>
  );
}

function ArchiDiagram() {
  return (
    <pre className="overflow-x-auto rounded-md bg-slate-950 px-4 py-3 text-[11px] leading-relaxed text-slate-50 dark:bg-slate-900">
{`┌─────────────────────────────────────────────────────────────┐
│  Frontend   Vite + React 18 + TypeScript + Tailwind        │
│             http://127.0.0.1:5173                          │
└───────────────────────────┬─────────────────────────────────┘
                            │  /api/*  /storage/*   (Vite proxy)
┌───────────────────────────▼─────────────────────────────────┐
│  Backend    FastAPI + SQLModel + APScheduler                │
│             http://127.0.0.1:8787                           │
│  ┌──────────┬──────────┬──────────┬──────────┬────────────┐ │
│  │ /papers  │ /search  │ /wiki    │ /sync    │ /process   │ │
│  │ /subs    │ /cite    │ /chat    │ /topics  │ /dedup     │ │
│  └────┬─────┴────┬─────┴────┬─────┴────┬─────┴─────┬──────┘ │
└───────┼──────────┼──────────┼──────────┼───────────┼────────┘
        │          │          │          │           │
        ▼          ▼          ▼          ▼           ▼
   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────────┐
   │ arXiv  │ │OpenAlex│ │   S2   │ │DeepSeek│ │  MinerU    │
   │ Atom   │ │ pyalex │ │ Graph  │ │ + Ark  │ │  PDF → MD  │
   └────────┘ └────────┘ └────────┘ └────────┘ └────────────┘
        │          │          │          │           │
        ▼          ▼          ▼          ▼           ▼
   ┌─────────────────────────────────────────────────────────┐
   │  PostgreSQL 16  +  pgvector  (chunks · wiki · papers)   │
   │  data/papers/<id>/{paper.pdf, paper.md, images/}        │
   │  data/wiki/{concepts,questions,scholars}/<slug>.md      │
   └─────────────────────────────────────────────────────────┘`}
    </pre>
  );
}

// ---------------------------------------------------------------------------
// Static data
// ---------------------------------------------------------------------------

const MILESTONES: Array<{
  id: string;
  name: string;
  status: "ok" | "wip";
  desc: string;
  bullets: string[];
}> = [
  {
    id: "m1",
    name: "骨架",
    status: "ok",
    desc: "Postgres+pgvector、FastAPI、React 三件套连通。",
    bullets: ["Docker Compose 起 pgvector:pg16", "FastAPI 空壳 + /health", "Vite + TS + Tailwind 模板", "YAML/env 配置 + 自动建表"],
  },
  {
    id: "m2",
    name: "抓取 + 库页",
    status: "ok",
    desc: "arXiv + OpenAlex 双源抓取，按 OpenAlex Work ID 归一化。",
    bullets: ["arXiv 关键词 / 分类订阅", "OpenAlex 元数据 + OA 下载源", "Work ID 去重，arXiv ID 兜底", "Subscription CRUD + sync jobs"],
  },
  {
    id: "m3",
    name: "PDF + MinerU",
    status: "ok",
    desc: "下载 OA PDF，HTTP 调 MinerU 解析成 Markdown。",
    bullets: ["OA PDF 下载（HTML 错答自愈）", "状态机 pending→pdf_ready→parsed", "MinerU 公式/表格/图片抽取", "Markdown 阅读器渲染"],
  },
  {
    id: "m4",
    name: "LLM 摘要",
    status: "ok",
    desc: "DeepSeek 主 + Ark fallback，链式生成双语摘要。",
    bullets: ["英文 TLDR + 中文 TLDR + 中文摘要", "关键词 + 主题分类", "S2 已有 TLDR 时保留，不重复生成", "失败不阻塞主流程"],
  },
  {
    id: "m5",
    name: "检索",
    status: "ok",
    desc: "切块 + 向量 + 全文 + RRF 融合排序。",
    bullets: ["800-1200 token 切块，重叠 150", "Ark embedding → vector(2048)", "向量 + tsvector 混合 RRF", "Search 页：本地/外部/语义三标签"],
  },
  {
    id: "m6",
    name: "定时 + 订阅 UI",
    status: "ok",
    desc: "APScheduler 跑 cron，订阅页可视化。",
    bullets: ["进程内 cron 调度", "Subscriptions 页 CRUD", "Nature/Cell/Science 顶刊一键", "Sync Log 页面 + 计划编辑"],
  },
  {
    id: "m7",
    name: "打磨",
    status: "wip",
    desc: "收藏/标签/笔记/引用/重试已就位，PDF 手动导入待补。",
    bullets: ["favorite · tags · notes (Markdown)", "Citations & References (S2)", "失败 Job 可重试", "manual PDF import (pending)"],
  },
  {
    id: "m8",
    name: "LLM Wiki",
    status: "wip",
    desc: "可重建的 Markdown 知识页，DB 只做索引。",
    bullets: ["scholar / concept / question 三类", "entity_key + redirect 壳", '用户笔记 <section data-user="true"> 保护', "Recompile 增量更新"],
  },
  {
    id: "m9",
    name: "Scholar 去重",
    status: "ok",
    desc: "OpenAlex 拆 A-ID 后的二阶段消歧。",
    bullets: ["共同作者 Jaccard + 机构 + 主题 + 名称", "scholar_aliases 表", "高置信度 auto-merge", "Duplicates UI 留人工裁决"],
  },
  {
    id: "m10",
    name: "Paper 去重",
    status: "wip",
    desc: "跨 ID 聚类 + LLM 评判。",
    bullets: ["DOI / arXiv / S2 / journal_doi 桥", "paper_aliases + PaperMergeEvent", "LLM judge on-demand 配对打分", "make migrate-paper-dedup 一键跑"],
  },
  {
    id: "m11",
    name: "知识图谱扩展",
    status: "wip",
    desc: "Topics 分类、per-paper Chat、Authors 反填。",
    bullets: ["Topics LLM 1-4 个共享词表", "per-paper RAG Chat (SSE)", "Authors Backfill (S2 → A-ID)", "Publication Check (arXiv → 期刊)"],
  },
];

const WORKFLOW: Array<{
  step: number;
  title: string;
  desc: string;
  href: string;
  icon: typeof BookOpen;
}> = [
  { step: 1, title: "添加订阅", desc: "关键词 / arXiv 分类 / 作者 A-ID / 顶刊 venue", href: "/subscriptions", icon: Rss },
  { step: 2, title: "一键同步", desc: "arXiv + OpenAlex fan-out，归一化去重入 inbox", href: "/sync", icon: Activity },
  { step: 3, title: "收纳入库", desc: "Today 卡片点 Import，主动控制 in_library 边界", href: "/today", icon: Library },
  { step: 4, title: "下载解析", desc: "Download & parse / Today 批量 Process pending", href: "/library", icon: FileText },
  { step: 5, title: "阅读 & 笔记", desc: "Markdown 主体 + 收藏/标签/Markdown 笔记", href: "/library", icon: BookText },
  { step: 6, title: "全局检索", desc: "OpenAlex + S2 + arXiv 融合，库内 Full-text 向量", href: "/", icon: Search },
  { step: 7, title: "进阶扩展", desc: "Topics · Citations · Chat · Wiki · Dedup", href: "/wiki", icon: Sparkles },
];

const ADVANCED: Array<{
  title: string;
  desc: string;
  icon: typeof BookOpen;
  href?: string;
}> = [
  { title: "多源聚合搜索", desc: "OpenAlex + Semantic Scholar + arXiv 三源 RRF 融合，字段权威排序（venue/tldr/pdf 各有优先级）。", icon: Search, href: "/" },
  { title: "全文混合检索", desc: "chunks 上做 vector cosine + tsvector 关键词，再 RRF；命中 chunk 直接回链到论文锚点。", icon: FileSearch, href: "/" },
  { title: "Paper Chat (RAG)", desc: "论文右侧栏助理式聊天，问题先在 chunks 上 top-K 再交 LLM，SSE 流式增量回显。", icon: MessageSquare },
  { title: "Scholar 去重", desc: "OpenAlex 经常把同一个人（尤其中国作者）拆成多个 A-ID；共同作者 + 机构 + 名称 + LLM 多维评分。", icon: Users, href: "/scholars" },
  { title: "Paper 去重", desc: "Library 顶部 Duplicates 面板：跨 ID 聚类 + LLM judge 配对打分；高置信度自动 merge。", icon: Layers, href: "/library" },
  { title: "Topics 分类", desc: "LLM 给每篇标 1-4 个共享词表主题；侧栏 checkbox 筛选，深度链接 ?topic= 跨页生效。", icon: Tag, href: "/topics" },
  { title: "Citations & References", desc: "S2 拉来的引用与参考文献；每行 Import 按钮一键把 cited-by 拉入 inbox / 库。", icon: Newspaper },
  { title: "LLM Wiki", desc: "scholar / concept / question 三类可重建 Markdown 页；用户笔记受 data-user 段保护不被覆盖。", icon: BookOpen, href: "/wiki" },
  { title: "Publication Check", desc: "arXiv → 期刊正式版探测；命中后自动拉 journal DOI 与发布日期，标记 pdf_origin。", icon: Telescope },
  { title: "Authors Backfill", desc: "S2 缩写作者名 → OpenAlex A-ID 反查，把历史库里的 author string 升级为可合并实体。", icon: Network },
  { title: "Inbox / Library 双层", desc: "papers.in_library 是库边界；sync 永远写 in_library=False，用户显式 Import 才入库。", icon: Library },
  { title: "失败可重试", desc: "每个 stage 独立 Job（16+ 种 kind），任何一步 failed 都可以从 Sync 页 / PaperDetail 重跑。", icon: Wrench, href: "/sync" },
];

const SOURCES = [
  { kind: "预印本", name: "arXiv Atom API", use: "关键词 / 分类 / 24h 时效", cred: "无 key · 强制限速 ≥ 3s" },
  { kind: "元数据脊梁", name: "OpenAlex", use: "Works / Authors / Sources / best_oa_location", cred: "OPENALEX_MAILTO（礼貌池）" },
  { kind: "引用网络", name: "Semantic Scholar Graph", use: "citationCount / references / cited-by / TLDR", cred: "可选 S2_API_KEY（有 key 提速）" },
  { kind: "LLM", name: "DeepSeek Chat", use: "摘要 / 关键词 / Topics / Wiki / Chat", cred: "DEEPSEEK_API_KEY" },
  { kind: "LLM 备用", name: "火山 Ark Doubao", use: "DeepSeek 失败时 fallback", cred: "VOLCANO_API_KEY" },
  { kind: "Embedding", name: "Ark doubao-embedding", use: "2048 维 · chunks + wiki 都用", cred: "VOLCANO_API_KEY" },
  { kind: "PDF 解析", name: "MinerU", use: "PDF → Markdown（公式/表格/图片）", cred: "独立进程 · 无 key" },
  { kind: "机构下载", name: "SSH 跳板 + scansci-pdf", use: "付费墙后 PDF / arXiv→期刊版", cred: "REMOTE_SSH_* 配置块" },
];

const MAKE_TARGETS = [
  {
    group: "依赖",
    rows: [
      { cmd: "install", desc: "后端 + 前端一次装齐" },
      { cmd: "install-backend", desc: "uv sync（缺 uv 回退 pip）" },
      { cmd: "install-frontend", desc: "cd frontend && npm install" },
    ],
  },
  {
    group: "数据库",
    rows: [
      { cmd: "make up", desc: "docker compose up -d postgres" },
      { cmd: "make down", desc: "停所有 compose 服务" },
      { cmd: "make psql", desc: "进 Postgres CLI" },
      { cmd: "make logs", desc: "tail 100 行 compose 日志" },
    ],
  },
  {
    group: "MinerU",
    rows: [
      { cmd: "make mineru-install", desc: "首次装 mineru[core] + 模型 (~2.5 GB)" },
      { cmd: "make mineru-up", desc: "后台起 mineru-api :8000" },
      { cmd: "make mineru-down", desc: "杀 mineru-api" },
      { cmd: "make mineru-build-gpu", desc: "Linux + NVIDIA：构建官方 mineru:latest" },
    ],
  },
  {
    group: "开发",
    rows: [
      { cmd: "make backend", desc: "前台 uvicorn :8787 --reload" },
      { cmd: "make frontend", desc: "前台 Vite :5173" },
      { cmd: "make start / stop / restart", desc: "detached 双服务 + 等健康" },
      { cmd: "make status", desc: "列出监听端口" },
    ],
  },
  {
    group: "健康",
    rows: [
      { cmd: "make doctor", desc: "只读健康检查" },
      { cmd: "make heal", desc: "安全自动修复" },
    ],
  },
  {
    group: "维护",
    rows: [
      { cmd: "make migrate-paper-dedup", desc: "一键扫 + auto-merge 高置信度重复" },
    ],
  },
  {
    group: "测试 / Lint",
    rows: [
      { cmd: "pytest", desc: "全跑 SQLite，不需 Docker" },
      { cmd: "ruff check carrel/ tests/", desc: "lint" },
      { cmd: "cd frontend && npm run lint", desc: "tsc --noEmit" },
    ],
  },
];

const PORTS = [
  { name: "Frontend", bind: "0.0.0.0", port: 5173, role: "Vite dev server · 代理 /api/* 与 /storage/*" },
  { name: "Backend", bind: "127.0.0.1", port: 8787, role: "FastAPI · Uvicorn · --reload in dev" },
  { name: "Postgres", bind: "127.0.0.1", port: 5432, role: "pgvector/pgvector:pg16 (docker compose)" },
  { name: "MinerU", bind: "127.0.0.1", port: 8000, role: "PDF→MD 解析（可选，独立进程）" },
];

// ---------------------------------------------------------------------------
// Section bodies
// ---------------------------------------------------------------------------

function OverviewBody() {
  return (
    <div className="space-y-4">
      <p className="text-base text-muted-foreground">
        Carrel 把你日常订阅的几个会议 / 关键词 / 作者，编排成一条本机流水线：
        <strong className="text-foreground">抓 → 解析 → 摘要 → 切块 → 检索 → 知识网</strong>。
        全程单机、单 Postgres、单 MinerU，可整盘打包带走。
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="space-y-2">
            <Rss className="h-5 w-5 text-primary" />
            <CardTitle className="text-base">自动入库</CardTitle>
            <CardDescription>关键词、arXiv 分类、作者 A-ID、顶刊 venue — 四种订阅类型 + 顶刊一键。</CardDescription>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="space-y-2">
            <Bot className="h-5 w-5 text-primary" />
            <CardTitle className="text-base">双语摘要</CardTitle>
            <CardDescription>DeepSeek 主、Ark 备；自动链式生成英文 TLDR + 中文 TLDR + 中文摘要 + 关键词。</CardDescription>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="space-y-2">
            <FileSearch className="h-5 w-5 text-primary" />
            <CardTitle className="text-base">全文检索</CardTitle>
            <CardDescription>向量 + tsvector 混合 RRF，三源（OpenAlex / S2 / arXiv）外部 + 库内全文。</CardDescription>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="space-y-2">
            <Network className="h-5 w-5 text-primary" />
            <CardTitle className="text-base">知识图谱</CardTitle>
            <CardDescription>Scholar / Concept / Question 三类 Wiki + per-paper RAG Chat + Topics 分类。</CardDescription>
          </CardHeader>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Carrel 的设计取舍</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-muted-foreground">
          <p>
            <Zap className="mr-1 inline h-3.5 w-3.5" />
            <strong className="text-foreground">失败不致命：</strong>
            每个 stage 独立 Job，失败可重试 — 一篇文章挂掉不会让整批卡住。
          </p>
          <p>
            <Library className="mr-1 inline h-3.5 w-3.5" />
            <strong className="text-foreground">Sync 是 inbox，不是入库：</strong>
            同步永远进 in_library=False；用户显式 Import 才进库，避免订阅范围过宽炸库。
          </p>
          <p>
            <Database className="mr-1 inline h-3.5 w-3.5" />
            <strong className="text-foreground">零云锁定：</strong>
            所有数据在本机 + Postgres；想迁 NAS 改一个 <Code>storage.root</Code> 即可。
          </p>
          <p>
            <Cpu className="mr-1 inline h-3.5 w-3.5" />
            <strong className="text-foreground">CPU / GPU 都行：</strong>
            Apple Silicon 走本地 MinerU 进程；Linux + NVIDIA 可改用官方 Docker 镜像。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function StackBody() {
  const cards = [
    { icon: Server, title: "后端", items: ["Python ≥ 3.11 + FastAPI + Uvicorn", "SQLModel · pydantic-settings · PyYAML", "httpx (含 SOCKS 代理) · pyalex · symspellpy", "litellm 统一接入 LLM / Embedding", "APScheduler 进程内 cron"] },
    { icon: Database, title: "数据库", items: ["PostgreSQL 16 + pgvector", "HNSW 索引 (vector(2048) / halfvec)", "chunks + tsvector 全文混合检索", "所有元数据 / Job / 状态机"] },
    { icon: Code2, title: "前端", items: ["Vite 5 + React 18 + TypeScript", "Tailwind 3.4 + shadcn 风格组件", "react-markdown + KaTeX (公式/表格/图)", "assistant-ui (per-paper RAG 聊天)", "lucide-react 图标系统"] },
    { icon: Brain, title: "AI & 解析", items: ["DeepSeek Chat（默认 LLM）", "火山 Ark Doubao（fallback + embedding）", "2048 维 · doubao-embedding-large", "MinerU（PDF → Markdown，独立进程）"] },
    { icon: Globe, title: "数据源", items: ["arXiv Atom API（关键词 / 分类）", "OpenAlex（Works / Authors / Sources）", "Semantic Scholar Graph（引用 / TLDR）", "可选：机构 SSH 跳板下载付费墙后 PDF"] },
    { icon: Cloud, title: "存储", items: ["data/config.yaml — 路径/模型/计划/订阅", "data/papers/<id>/{paper.pdf, paper.md}", "data/wiki/{concepts,questions,scholars}/", "Postgres — 元数据/状态/Job/向量", "/storage/* 静态挂载本地文件"] },
  ];
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map((c) => (
        <Card key={c.title}>
          <CardHeader className="space-y-2">
            <c.icon className="h-5 w-5 text-primary" />
            <CardTitle className="text-base">{c.title}</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1.5 text-sm text-muted-foreground">
              {c.items.map((it) => (
                <li key={it} className="flex items-start gap-2">
                  <span className="mt-1.5 inline-block h-1 w-1 shrink-0 rounded-full bg-muted-foreground/60" />
                  <span>{it}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function FeaturesBody() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {MILESTONES.map((m) => (
        <Card key={m.id}>
          <CardHeader className="space-y-2 pb-3">
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
                {m.id.toUpperCase()}
              </span>
              <Pill tone={m.status}>{m.status === "ok" ? "已完成" : "进行中"}</Pill>
            </div>
            <CardTitle className="text-base">{m.name}</CardTitle>
            <CardDescription>{m.desc}</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1.5 text-sm text-muted-foreground">
              {m.bullets.map((b) => (
                <li key={b} className="flex items-start gap-2">
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                  <span>{b}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function WorkflowBody() {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {WORKFLOW.map((w) => (
          <Link key={w.step} to={w.href} className="block">
            <Card className="h-full transition-colors hover:bg-muted/30">
              <CardHeader className="space-y-2 pb-3">
                <div className="flex items-center justify-between">
                  <span className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10 font-mono text-xs text-primary">
                    {w.step}
                  </span>
                  <w.icon className="h-4 w-4 text-muted-foreground" />
                </div>
                <CardTitle className="text-base">{w.title}</CardTitle>
                <CardDescription>{w.desc}</CardDescription>
              </CardHeader>
              <CardContent>
                <span className="inline-flex items-center text-xs text-primary">
                  打开 <ArrowRight className="ml-1 h-3 w-3" />
                </span>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">状态机与 Job 模型</CardTitle>
          <CardDescription>每个 stage 独立 Job，互不阻塞</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-muted-foreground">
          <p>
            论文状态机：<Code>pending → pdf_ready → parsed → summarized → ready</Code>，
            任一阶段失败 → 对应 <Code>failed_X</Code>，可从 Sync 页 / PaperDetail 重跑。
          </p>
          <p>
            Job kind 一共 16+ 种：<Code>sync · download · parse · summarize · topics · authors_backfill · embed · citations · remote_fill · publication_check · wiki_compile · wiki_recompile · scholar_dedup · paper_dedup · paper_extract</Code>。
          </p>
          <p>
            <Code>stats</Code> 字段含 <Code>paper_id / paper_title / stage / detail</Code>，
            前端 TaskList 侧栏轮询 <Code>/sync/jobs</Code> 实时跟踪。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function ArchitectureBody() {
  return (
    <div className="space-y-4">
      <ArchiDiagram />
      <div>
        <h3 className="mb-2 text-sm font-semibold">端口与服务</h3>
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="px-3 py-2">服务</th>
                <th className="px-3 py-2">端口</th>
                <th className="px-3 py-2">绑定</th>
                <th className="px-3 py-2">角色</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {PORTS.map((p) => (
                <tr key={p.name}>
                  <td className="px-3 py-2 font-medium">{p.name}</td>
                  <td className="px-3 py-2 font-mono text-xs">:{p.port}</td>
                  <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{p.bind}</td>
                  <td className="px-3 py-2 text-muted-foreground">{p.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">数据落盘位置</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1.5 text-sm text-muted-foreground">
          <p><Code>data/config.yaml</Code> — 用户配置（路径 / 模型 / 计划 / 订阅）。</p>
          <p><Code>data/papers/&lt;work_id&gt;/paper.pdf</Code> — 活跃 PDF（不可删除，否则解析图会断链）。</p>
          <p><Code>data/papers/&lt;work_id&gt;/paper.md</Code> + <Code>images/</Code> — MinerU 输出。</p>
          <p><Code>data/wiki/{`{concepts,questions,scholars}`}/&lt;slug&gt;.md</Code> — 编译出的 wiki 真源。</p>
          <p><Code>Postgres</Code> — 元数据 / Job / chunks（向量 + tsvector）/ 状态机 / 去重关系。</p>
        </CardContent>
      </Card>
    </div>
  );
}

function QuickStartBody() {
  return (
    <div className="space-y-3">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">前置依赖</CardTitle>
          <CardDescription>准备一次，永久使用</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-1 text-sm text-muted-foreground">
            <li>· Docker Desktop（macOS / Windows）或 Linux Docker Engine</li>
            <li>· Python ≥ 3.11（推荐用 <a className="text-primary underline underline-offset-2" href="https://docs.astral.sh/uv/" target="_blank" rel="noreferrer">uv</a>）</li>
            <li>· Node.js ≥ 20</li>
            <li>· API Key：<Code>DEEPSEEK_API_KEY</Code>、<Code>VOLCANO_API_KEY</Code>、<Code>OPENALEX_MAILTO</Code>（强烈建议）</li>
          </ul>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 1 步 · 准备配置</CardTitle>
        </CardHeader>
        <CardContent>
          <CodeBlock>{`cp .env.example .env                       # 填 API keys
mkdir -p data && cp config.example.yaml data/config.yaml`}</CodeBlock>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 2 步 · 起 Postgres + pgvector</CardTitle>
        </CardHeader>
        <CardContent>
          <CodeBlock>{`make up                                    # :5432`}</CodeBlock>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 3 步 · 装后端 + 起后端</CardTitle>
        </CardHeader>
        <CardContent>
          <CodeBlock>{`make install-backend                       # uv sync
make backend                               # http://127.0.0.1:8787/health`}</CodeBlock>
          <p className="mt-2 text-xs text-muted-foreground">
            首次启动会自动建表（<Code>init_db</Code>），无需 alembic upgrade。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 4 步 · 装前端 + 起前端（另开终端）</CardTitle>
        </CardHeader>
        <CardContent>
          <CodeBlock>{`make install-frontend
make frontend                              # http://127.0.0.1:5173`}</CodeBlock>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 5 步 ·（可选）MinerU PDF 解析</CardTitle>
          <CardDescription>首次需要下载约 2.5 GB 模型</CardDescription>
        </CardHeader>
        <CardContent>
          <CodeBlock>{`make mineru-install
make mineru-up                             # http://127.0.0.1:8000`}</CodeBlock>
          <p className="mt-2 text-xs text-muted-foreground">
            Linux + NVIDIA 机器可以用官方 GPU 镜像：<Code>make mineru-build-gpu</Code> 然后 <Code>docker compose --profile mineru up -d</Code>。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">第 6 步 · 添加订阅 & 同步</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            打开 <Link className="text-primary underline underline-offset-2" to="/subscriptions">/subscriptions</Link> 添加一个 keyword 或 arXiv 分类，再到 <Link className="text-primary underline underline-offset-2" to="/today">/today</Link> 顶部点 <strong className="text-foreground">Sync now (72h)</strong>。
            抓回来的论文默认在 inbox（in_library=False），在 Today 卡片点 <strong className="text-foreground">Import</strong> 显式入库。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function AdvancedBody() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {ADVANCED.map((a) => {
        const inner = (
          <Card className="h-full transition-colors hover:bg-muted/30">
            <CardHeader className="space-y-2 pb-3">
              <a.icon className="h-5 w-5 text-primary" />
              <CardTitle className="text-base">{a.title}</CardTitle>
              <CardDescription>{a.desc}</CardDescription>
            </CardHeader>
          </Card>
        );
        return a.href ? (
          <Link key={a.title} to={a.href} className="block">
            {inner}
          </Link>
        ) : (
          <div key={a.title}>{inner}</div>
        );
      })}
    </div>
  );
}

function SourcesBody() {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="px-3 py-2">类别</th>
            <th className="px-3 py-2">名称</th>
            <th className="px-3 py-2">用途</th>
            <th className="px-3 py-2">凭据 / 备注</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {SOURCES.map((s) => (
            <tr key={s.name}>
              <td className="px-3 py-2 text-muted-foreground">{s.kind}</td>
              <td className="px-3 py-2 font-medium">{s.name}</td>
              <td className="px-3 py-2 text-muted-foreground">{s.use}</td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground">{s.cred}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OpsBody() {
  return (
    <div className="space-y-3">
      {MAKE_TARGETS.map((g) => (
        <Card key={g.group}>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{g.group}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full text-sm">
                <tbody className="divide-y">
                  {g.rows.map((r) => (
                    <tr key={r.cmd}>
                      <td className="w-2/5 whitespace-nowrap px-3 py-2 align-top font-mono text-xs">
                        {r.cmd}
                      </td>
                      <td className="px-3 py-2 align-top text-muted-foreground">
                        {r.desc}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function LicenseBody() {
  return (
    <div className="space-y-3">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">本项目</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <p>
            Carrel 自身代码使用 <strong className="text-foreground">MIT</strong> 许可证 — 你可以自由使用、修改、再发布。
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">MinerU</CardTitle>
          <CardDescription>PDF → Markdown 解析引擎</CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <p>
            MinerU 本身是 <strong className="text-foreground">AGPL-3.0</strong>。
            Carrel 通过独立 HTTP 进程调用 MinerU（不在同一进程空间链接），
            因此 <strong className="text-foreground">AGPL 不会传染到 Carrel 自身代码</strong>。
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">上游参考项目</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm text-muted-foreground">
          <p>· paper-agent — MIT</p>
          <p>· pyalex — MIT</p>
          <p>· react-markdown / KaTeX / assistant-ui — 各自的 MIT/Apache 许可证</p>
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section registry
// ---------------------------------------------------------------------------

type SectionDef = {
  id: string;
  label: string;
  icon: typeof BookOpen;
  title: string;
  subtitle: string;
  body: () => ReactNode;
};

const SECTIONS: SectionDef[] = [
  { id: "overview", label: "项目概览", icon: BookOpen, title: "1 · 项目概览", subtitle: "一个人、单机、单 Postgres 的论文研读间 — 不登录、无云锁定、可整盘打包带走。", body: OverviewBody },
  { id: "stack", label: "技术栈", icon: Code2, title: "2 · 技术栈", subtitle: "Carrel 把外部世界（arXiv、OpenAlex、S2、LLM、MinerU）编排成一条本机流水线，所有产物都留在你的硬盘与 Postgres 里。", body: StackBody },
  { id: "features", label: "功能路线图", icon: ListChecks, title: "3 · 功能路线图", subtitle: "M1-M11 共 11 个里程碑，绿色为已上线、琥珀色为持续打磨中。", body: FeaturesBody },
  { id: "workflow", label: "每日工作流", icon: Activity, title: "4 · 每日核心工作流", subtitle: "订阅→同步→入库→解析→阅读→检索，每个阶段都可独立重试，失败不会卡住整批。", body: WorkflowBody },
  { id: "architecture", label: "系统架构", icon: Server, title: "5 · 系统架构", subtitle: "前端与后端都在本机，外部服务（arXiv / OpenAlex / S2 / DeepSeek / Ark / MinerU）都是 HTTP 客户端。", body: ArchitectureBody },
  { id: "quickstart", label: "快速开始", icon: Terminal, title: "6 · 快速开始", subtitle: "5 分钟把 Carrel 跑起来：先起数据库，再起后端，最后起前端；MinerU 是可选的解析服务。", body: QuickStartBody },
  { id: "advanced", label: "进阶功能", icon: Sparkles, title: "7 · 进阶功能", subtitle: "M7 之后的所有能力都在这里 — 让一个『论文 PDF 集合』长成一张可探索的知识网。", body: AdvancedBody },
  { id: "sources", label: "数据源", icon: Globe, title: "8 · 数据源与依赖", subtitle: "所有外部服务都通过 HTTP 调用；凭据放在 .env，配置放在 data/config.yaml。", body: SourcesBody },
  { id: "ops", label: "运维命令", icon: Wrench, title: "9 · 运维命令", subtitle: "所有命令都在 Makefile 里；新装的 make help 也能看到完整列表。", body: OpsBody },
  { id: "license", label: "许可", icon: FileText, title: "10 · 许可", subtitle: "本项目自身 MIT；MinerU 走独立进程调用，AGPL-3.0 不会传染到我们的代码。", body: LicenseBody },
];

// ---------------------------------------------------------------------------
// Sidebar (sticky TOC, desktop only)
// ---------------------------------------------------------------------------

function Sidebar() {
  return (
    <aside className="hidden md:block">
      <div className="sticky top-20">
        <p className="mb-2 px-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
          本页目录
        </p>
        <nav className="space-y-0.5">
          {SECTIONS.map((s, idx) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className="group flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <s.icon className="h-3.5 w-3.5 shrink-0" />
              <span className="tabular-nums text-xs text-muted-foreground/70">
                {String(idx + 1).padStart(2, "0")}
              </span>
              <span>{s.label}</span>
            </a>
          ))}
        </nav>
        <p className="mt-6 px-2 text-xs text-muted-foreground">
          想了解每个功能背后的工程细节，可参考 <Code>README.md</Code> 与 <Code>PLAN.md</Code>。
        </p>
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Docs() {
  return (
    <main className="container max-w-screen-2xl py-8">
      <div className="grid grid-cols-1 gap-8 md:grid-cols-[224px_minmax(0,1fr)]">
        <Sidebar />
        <div className="min-w-0 space-y-12">
          {/* Page header */}
          <header className="space-y-3">
            <div className="flex items-center gap-3">
              <BookOpen className="h-7 w-7 text-primary" />
              <h1 className="text-3xl font-bold tracking-tight">项目文档</h1>
            </div>
            <p className="max-w-3xl text-base text-muted-foreground">
              Carrel 是什么、能做什么、怎么用、怎么跑 — 一次看完。
              技术细节参考 <Code>README.md</Code> / <Code>PLAN.md</Code>，开发约定参考 <Code>docs/architecture.md</Code>。
            </p>
          </header>

          {/* Sections */}
          {SECTIONS.map((s) => {
            const Body = s.body;
            return (
              <section key={s.id} id={s.id} className="scroll-mt-20 space-y-4">
                <div>
                  <h2 className="text-2xl font-semibold tracking-tight">{s.title}</h2>
                  <p className="mt-1 text-sm text-muted-foreground">{s.subtitle}</p>
                </div>
                <Body />
              </section>
            );
          })}

          <footer className="border-t pt-6 text-xs text-muted-foreground">
            <p>
              文档由 <Code>Docs.tsx</Code> 维护 · 想补充内容直接改{" "}
              <Code>frontend/src/pages/Docs.tsx</Code>。
            </p>
          </footer>
        </div>
      </div>
    </main>
  );
}
