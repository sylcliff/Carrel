---
name: ops
description: Carrel 本地运维 — 诊断和自愈网络/服务问题（端口、健康检查、Tailscale Serve、系统代理绕过列表被闪狐云重写导致的 502）。当用户说服务打不开、502、Tailscale 连不上、端口被占、检查/拉起服务、"运维"、"监控"、或想知道 Carrel 是否正常运行时使用。
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Carrel Ops

监控并自动化运维 Carrel 本地开发栈。所有脚本自包含在本 skill 下的 `scripts/` 目录（`.claude/skills/ops/scripts/`），脚本内部用自身位置解析绝对路径，因此**从任意 cwd 调用都安全**。在 shell 里直接敲相对路径仍依赖当前目录——优先用下面的 `make` 入口（make 总是切到仓库根目录执行），或用绝对路径 `/Users/syl/code/Carrel/.claude/skills/ops/scripts/...`，或用本 skill 的 `scripts/...` 相对路径（skill 基目录固定）。

> **cwd 陷阱**：本会话里的 Bash 工作目录会在多次调用间保持。如果之前跑过 `cd frontend`，相对路径会失败（exit 127）。优先用 `make` 目标，或先 `cd /Users/syl/code/Carrel`。

## 拓扑

- **唯一对外入口**：Vite `0.0.0.0:5173`（把 `/api`、`/storage` 代理到后端）。
- 后端 FastAPI `127.0.0.1:8787`（`/health` 返回 `db` 状态）。
- Postgres：Docker 容器 `carrel-postgres`，端口 5432（`make up`）。
- MinerU（可选）：`127.0.0.1:8000`，PDF 解析（`make mineru-up`）。
- **推荐对外入口**：Tailscale Serve —— `https://<node>.<tailnet>.ts.net`（tailscaled 内置反代 + 自动 HTTPS，转发到 5173）。实际域名由 `tailscale status`（Self DNSName）动态获取；脚本里用 `public_url()`。

## 已知故障模式（按频率）

1. **系统代理绕过列表被重写 → 502/超时（最常见）**。GUI 代理客户端「闪狐云 / FlashFox」(`127.0.0.1:7892`) 在重连/切节点时会重写系统代理绕过列表，丢掉 `100.*`（Tailscale CGNAT 段）和 `*.ts.net`，于是浏览器把 tailnet 流量发给代理 → 502。绕过列表权威来源是 `.claude/skills/ops/scripts/bypass-domains.txt`。
2. **Tailscale Serve 未启用**：没有它，浏览器只能用 `http://100.x:5173`，且必须保证绕过列表正确。启用一次即可，配置由 tailscaled 持久化。
3. **dev server 没起**（backend 或 frontend）：用 `make restart` 一条命令同时拉起两个并等待健康检查通过。注意前端挂了后端可能还活着，反之亦然——只重启一个不算完成。
4. **Tailscale 走 DERP 中继**：能用但延迟高；`tailscale ping` 显示 "direct connection not established"。

## 工作流

### 一键命令（优先用这些）

```bash
make restart    # 停掉并以后台方式重启 backend+frontend，等待 /health 通过才返回
make start      # 只启动（已在跑的跳过）
make stop       # 停掉 backend+frontend
make status     # 看两个 dev server 的监听状态
make doctor     # 只读体检；异常返回非零
make heal       # 安全自愈（代理绕过列表）
```

`make restart/start` 把日志写到 `/tmp/carrel-ops/backend.log` 和 `frontend.log`，并轮询后端 `/health`、前端 `/` 和前端 `/api/health` 代理，三者都 200 才报告成功。**重启后必须确认 doctor 全绿（Tailscale Serve 那条可接受），否则不要对用户说"好了"。**

### 体检（只读，任何排障第一步）

```bash
make doctor                                       # 人类可读
.claude/skills/ops/scripts/doctor.sh --json       # 给程序/skill 解析
```

检查：端口监听、后端 `/health`、Vite 及 `/api` 代理、Tailscale 守护进程与 Serve、各网络服务的代理绕过列表、端到端直连 IP 与 HTTPS 域名。

### 自愈

```bash
make heal                                       # 安全地自动修复 + 给出需手动处理的命令
.claude/skills/ops/scripts/heal.sh --dry-run    # 只看不改
```

**自动修复（安全）**：把权威绕过列表重新写回所有物理网络服务（Wi-Fi/Ethernet 等）。这是反复出现的 502 的对症修复。

**heal 不管 dev server**——用 `make start/restart` 拉起前后端。

**只提示、不自动改**：
- 启用 Tailscale Serve 需要一次性 GUI/sudo 授权——给出命令让用户在自己的终端跑：
  `"/Applications/Tailscale.app/Contents/MacOS/Tailscale" serve --bg 5173`
- Postgres / MinerU（Docker/native，用户用 `make up` / `make mineru-up` 管理）。

### 后台自愈守护（自动化的核心）

```bash
.claude/skills/ops/scripts/install-agent.sh install    # 装 launchd 代理，每 60s 跑 heal.sh --agent
.claude/skills/ops/scripts/install-agent.sh status     # 看是否在跑、最近修复记录
.claude/skills/ops/scripts/install-agent.sh uninstall  # 移除
```

launchd 每 60 秒以当前用户身份跑一次 `heal.sh --agent`：绕过列表被闪狐云重写时自动补回，**没事时完全安静**，修复动作写入 `/tmp/carrel-ops/heal.log`。这让 "tailnet 访问突然 502" 这类问题在 60 秒内自愈，无需人工介入。注意：该守护**只修代理绕过，不拉起 dev server**。

启用 Tailscale Serve 是更彻底的根治（标准 443 + `*.ts.net` 已在绕过列表），与本守护互补。

## 给 agent 的排障指引

收到 "重启/打不开/502/连不上/Failed to fetch" 时，**按顺序**：

1. **先 `make doctor`**（或 `.claude/skills/ops/scripts/doctor.sh --json`）定位层级，别用一堆 `lsof`/`ps`/`curl` 手搓——doctor 一份报告就给全。若 cwd 不对导致相对路径失败，用 make 或绝对路径。
2. 用户要求"重启"时，**直接 `make restart`**（backend 和 frontend 一起），不要只重启后端。它会等待健康检查；若失败，读 `/tmp/carrel-ops/*.log`。
3. 重启后**再跑一次 `make doctor`** 确认：backend、frontend、`/api` 代理这三条必须是 ✔，才能告诉用户完成。Tailscale Serve 那条未配置是已知可选警告，不阻塞。
4. 若是 proxy bypass 缺失：跑 `make heal`；若 agent 未装，建议 `.claude/skills/ops/scripts/install-agent.sh install` 防复发。
5. 若 doctor 提示 Tailscale Serve 未配置：**不要**在后台/非交互环境直接跑 `tailscale serve`（会因无法弹授权窗而失败），把那条命令交给用户用 `!` 前缀在他们的终端执行。
6. Postgres 没起 → `make up`；MinerU 没起 → `make mineru-up`。
7. 改了绕过列表后，浏览器可能需要重启才重读系统代理设置。

## 端口/路径速查

| 服务 | 地址 |
|---|---|
| 对外（推荐） | `https://<node>.<tailnet>.ts.net`（Tailscale Serve，域名见 `tailscale status`） |
| 对外（直连） | `http://<tailscale-ipv4>:5173`（IP 见 `tailscale ip -4`） |
| 本机前端 | http://127.0.0.1:5173 |
| 后端健康 | http://127.0.0.1:8787/health |
| 日志 | `/tmp/carrel-ops/backend.log`、`/tmp/carrel-ops/frontend.log`、`/tmp/carrel-ops/heal.log` |

绕过列表新增条目：编辑 `.claude/skills/ops/scripts/bypass-domains.txt`（每行一个，shell glob，非 CIDR），再跑 `make heal`。
