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

监控并自动化运维 Carrel 本地开发栈。所有脚本在 `scripts/ops/`，可直接命令行运行，也可由此 skill 调用。

## 拓扑

- **唯一对外入口**：Vite `0.0.0.0:5173`（把 `/api`、`/storage` 代理到后端）。
- 后端 FastAPI `127.0.0.1:8787`（`/health` 返回 `db` 状态）。
- Postgres：Docker 容器 `carrel-postgres`，端口 5432。
- MinerU（可选）：`127.0.0.1:8000`，PDF 解析。
- **推荐对外入口**：Tailscale Serve —— `https://<node>.<tailnet>.ts.net`（tailscaled 内置反代 + 自动 HTTPS，转发到 5173）。实际域名由 `tailscale status`（Self DNSName）动态获取；脚本里用 `public_url()`。

## 已知故障模式（按频率）

1. **系统代理绕过列表被重写 → 502/超时（最常见）**。GUI 代理客户端「闪狐云 / FlashFox」(`127.0.0.1:7892`) 在重连/切节点时会重写系统代理绕过列表，丢掉 `100.*`（Tailscale CGNAT 段）和 `*.ts.net`，于是浏览器把 tailnet 流量发给代理 → 502。绕过列表权威来源是 `scripts/ops/bypass-domains.txt`。
2. **Tailscale Serve 未启用**：没有它，浏览器只能用 `http://100.x:5173`，且必须保证绕过列表正确。启用一次即可，配置由 tailscaled 持久化。
3. **进程没起 / 端口被旧进程占用**：backend/frontend 是裸后台进程，重启后需手动拉起。
4. **Tailscale 走 DERP 中继**：能用但延迟高；`tailscale ping` 显示 "direct connection not established"。

## 工作流

### 体检（只读，优先做这个）

```bash
scripts/ops/doctor.sh           # 人类可读；异常返回非零
scripts/ops/doctor.sh --json    # 给程序/skill 解析
```

检查：端口监听、后端 `/health`、Vite 及 `/api` 代理、Tailscale 守护进程与 Serve、各网络服务的代理绕过列表、端到端直连 IP 与 HTTPS 域名。

### 自愈

```bash
scripts/ops/heal.sh           # 安全地自动修复 + 给出需手动处理的命令
scripts/ops/heal.sh --dry-run # 只看不改
```

**自动修复（安全）**：把权威绕过列表重新写回所有物理网络服务（Wi-Fi/Ethernet 等）。这是反复出现的 502 的对症修复。

**只提示、不自动改**：
- 启用 Tailscale Serve 需要一次性 GUI/sudo 授权——给出命令让用户在自己的终端跑：
  `"/Applications/Tailscale.app/Contents/MacOS/Tailscale" serve --bg 5173`
- 启停 backend/frontend/postgres（用户当前选择手动管理）。

### 后台自愈守护（自动化的核心）

```bash
scripts/ops/install-agent.sh install    # 装 launchd 代理，每 60s 跑 heal.sh --agent
scripts/ops/install-agent.sh status     # 看是否在跑、最近修复记录
scripts/ops/install-agent.sh uninstall  # 移除
```

launchd 每 60 秒以当前用户身份跑一次 `heal.sh --agent`：绕过列表被闪狐云重写时自动补回，**没事时完全安静**，修复动作写入 `/tmp/carrel-ops/heal.log`。这让 "tailnet 访问突然 502" 这类问题在 60 秒内自愈，无需人工介入。

启用 Tailscale Serve 是更彻底的根治（标准 443 + `*.ts.net` 已在绕过列表），与本守护互补。

## 给 agent 的排障指引

收到 "打不开/502/连不上" 时，按顺序：

1. 先 `scripts/ops/doctor.sh`（或 `--json` 解析）定位层级，别猜。
2. 若是 proxy bypass 缺失：跑 `scripts/ops/heal.sh`；若 agent 未装，建议 `install-agent.sh install` 防复发。
3. 若 doctor 提示 Tailscale Serve 未配置：**不要**在后台/非交互环境直接跑 `tailscale serve`（会因无法弹授权窗而失败），把那条命令交给用户用 `!` 前缀在他们的终端执行。
4. 若某个服务端口没监听：提醒用户对应命令（`make backend` / `cd frontend && npm run dev` / `make up` / `make mineru-up`）。当前不自动拉起这些进程。
5. 改了绕过列表后，浏览器可能需要重启才重读系统代理设置。

## 端口/路径速查

| 服务 | 地址 |
|---|---|
| 对外（推荐） | `https://<node>.<tailnet>.ts.net`（Tailscale Serve，域名见 `tailscale status`） |
| 对外（直连） | `http://<tailscale-ipv4>:5173`（IP 见 `tailscale ip -4`） |
| 本机前端 | http://127.0.0.1:5173 |
| 后端健康 | http://127.0.0.1:8787/health |

绕过列表新增条目：编辑 `scripts/ops/bypass-domains.txt`（每行一个，shell glob，非 CIDR），再跑 `heal.sh`。
