## A500 Trader 前端

Next.js App Router 工作台，调用仓库唯一的 Python REST 业务后端，不实现第二套业务 API 或 OAuth token broker。

### 统一本地启动

在仓库根目录按 [根 README](../README.md#本地前后端集成) 配置 AmazingData、临时 Keycloak 管理员凭据和行情模式，然后执行：

```bash
python3 scripts/start-local.py
```

打开 **<http://localhost:8080>**（不要替换为 `127.0.0.1`）；本地 Keycloak 为 `http://localhost:8081`，必须由管理员在 `a500` Realm 创建获准用户。没有默认用户或注册入口。启动器构建前端和 Python 服务，保持 RabbitMQ/Redis 原有拓扑；不是 mock 或行情采集器。

### 开发与公开配置

Bun 与 Python/uv 分别管理依赖。修改前端后，在仓库根目录重新执行 `python3 scripts/start-local.py`，构建并重建 Compose 服务。网关将 UI 与 API 统一暴露在同一来源；不支持通过宿主机独立 Next 开发服务器访问 API。

- 浏览器使用同源 `/api/v1/*`，Nginx 网关转发至业务 API；Bearer、ETag 前置条件、CSV multipart 与 SSE 游标均经由网关传递。Next 不承担 API 代理，不需要 API CORS。
- `NEXT_PUBLIC_A500_OIDC_ISSUER`：浏览器可访问的 issuer；集成默认 `http://localhost:8081/realms/a500`。discovery 必须提供 authorization endpoint 和 token endpoint。
- `NEXT_PUBLIC_A500_OIDC_CLIENT_ID`：默认 `a500-browser-client`。IdP public client 必须启用 Authorization Code 和 PKCE S256，精确允许前端来源及根路径回调 URI；仓库 Realm 默认允许 `http://localhost:8080`。
- SSE 断线后由客户端携带游标重新连接。

公开变量写入 production build；修改后需重新构建，不能只改运行时容器环境。`NEXT_PUBLIC_*` 不得放客户端密钥。缺少服务配置时应按页面提示设置配置，不会假装后端可用。自定义来源/外部 IdP 请直接配置 Compose 与 public client，不要混用 `localhost` 和 `127.0.0.1`。

### 身份与工作流

前端通过 Authorization Code + PKCE 跳转 Keycloak 登录，用户密码仅输入 Keycloak，不再使用设备码。访问令牌仅留在当前页面内存，不写 localStorage、URL 或日志；刷新或失效后需重新登录，有效的 Keycloak 会话可免去重新输入密码。当前前端不使用 refresh token 自动续期；A500 API 仅验签，不签发/刷新 token。局域网 HTTP 仅适合可信网络，生产环境应使用 HTTPS。

工作台提供：

- 当前信号查询及 SSE，断线携带 `Last-Event-ID` 续传；`reset` 清理过期状态并读取当前快照。数据可能不可用或过期，不代表下单。
- `/signals` 使用共享 App Shell 与桌面优先的响应式导航，导航覆盖 `/assets`、`/data/trades` 和 `/data/history`。标的筛选以重复的 `symbols` 参数传给 REST 与 SSE。
- 信号行展示策略/规则身份、参数指纹及标的参数；`stale` 或 `UNAVAILABLE` 时不把方向/数量显示为当前建议，`execution_eligible=false` 明确标示。页面不提供下单入口。
- `/assets` 展示服务端完整 NAV 估值与账户快照；NAV 不可用时展示原因，不自行合计。六个快照字段只读查看、显式编辑；金额按十进制字符串校验并保留超大数值精度，重复提交受保护。保存使用 `If-None-Match`/`If-Match`，冲突保留草稿并允许读取最新版本后选择覆盖方式；保存失败保留输入，放弃脏草稿前提供一次确认。
- 银河证券 CSV 预览、逐行脱敏错误、显式确认及同一主体的批次恢复。客户端拒绝空文件、非 `.csv` 文件和超过 5 MiB 的文件；服务端仍负责最终解析与校验。上传可取消，校验错误阻止确认；存在重复记录时必须先确认这些记录不会再次写入。确认响应中断后先查询原批次，只有服务端明确报告完成才展示成功；查询不可用时保留原批次并提示不要重新上传。仅保存恢复所需的批次标识，不持久化原始 CSV、token 或账户数据；刷新后重登原身份可读取服务端预览结果。导入不更新账户快照。
- `/data/history` 只提交后端支持的日线任务，并以真实后端状态、分片进度和失败原因更新；长时间无后端更新时间时只提示仍在查询，不伪报失败。每次任务状态/结果读取以及创建、取消请求限时 15 秒；失败保留最后确认状态或显示“状态未知”任务编号，并提供重试。提交超时会明确警告后端可能已创建任务但没有返回编号，提示勿盲目重复提交。成功任务读取真实分页结果，明确显示空结果；结果续读失败时标记此前数据可能不是最新页。快速重复点击不会并发提交。后端不提供任务列表、幂等键或重试接口：浏览器保存最多 30 个任务编号以便同一浏览器刷新恢复；重新拉取会创建新任务。历史行情持久化仍需部署者授权。

账户数据由用户录入、未经券商核验；没有下单、账户历史或部署管理入口。
入口 `/` 仅负责 OIDC 根路径回调；普通访问会进入 `/signals`。授权回调验证成功后返回发起连接的 `/signals`、`/assets`、`/data/trades` 或 `/data/history` 页面，无法识别的目标回退到 `/signals`。各页的“断开”会清除当前标签页内存令牌并返回策略信号；不会注销 Keycloak 浏览器会话。

### 测试

- `bun run test`：单元测试。Vitest 配置使用 `vitest.config.mts`，以便 Vite 的原生配置加载器按 ES module 加载。
- `bun run test:e2e`：Playwright；浏览器准备命令按仓库 Playwright 配置/根验收说明执行。可控行情 fixture 是测试数据，不是实时行情或实际 IdP 证明。
  当设置 `NO_COLOR` 时，测试进程将其转换为 `FORCE_COLOR=0`，避免 Playwright 子进程的颜色变量冲突；Playwright 自身仍会强制测试 worker 输出颜色。
  并行开发端口被占用时可设置 `A500_E2E_FIXTURE_PORT`、`A500_E2E_WEB_PORT` 与 `A500_E2E_NEXT_PORT`（默认 Web 端口加一）；测试专用网关把 UI 和 API 汇集到同一来源，不用于生产。测试产物按 Web 端口隔离。独立 Next 实例可用 `A500_NEXT_DIST_DIR` 隔离构建目录。
- 真实 Keycloak/设备授权/API 验收命令见 [信号 API 文档](../docs/signal-monitoring-api.md#compose-真实端到端验收)。
