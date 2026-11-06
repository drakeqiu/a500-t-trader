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
- `NEXT_PUBLIC_A500_OIDC_ISSUER`：浏览器可访问的 issuer；集成默认 `http://localhost:8081/realms/a500`。discovery 必须提供设备授权与 token endpoint。
- `NEXT_PUBLIC_A500_OIDC_CLIENT_ID`：默认 `a500-device-client`。IdP public client 的 `webOrigins` 必须精确允许前端来源，仓库 Realm 默认允许 `http://localhost:8080`，不使用通配符。
- SSE 断线后由客户端携带游标重新连接。

公开变量写入 production build；修改后需重新构建，不能只改运行时容器环境。`NEXT_PUBLIC_*` 不得放客户端密钥。缺少服务配置时应按页面提示设置配置，不会假装后端可用。自定义来源/外部 IdP 请直接配置 Compose 与 public client，不要混用 `localhost` 和 `127.0.0.1`。

### 身份与工作流

前端展示设备码并按 IdP 间隔轮询；用户密码仅输入 Keycloak。访问令牌仅留在当前页面内存，不写 localStorage、URL 或日志；刷新页面后必须重新授权。当前前端不使用 refresh token 自动续期；过期、401 或撤销后重新设备授权。A500 API 仅验签，不签发/刷新 token。

工作台提供：

- 当前信号查询及 SSE，断线携带 `Last-Event-ID` 续传；`reset` 清理过期状态并读取当前快照。数据可能不可用或过期，不代表下单。
- 当前主体的账户快照读取、首次创建与完整替换；保存使用 `If-None-Match`/`If-Match`，冲突保留本地草稿，不静默覆盖远端。
- NAV 完整可用才显示净资产；缺失、未来、过期行情或快照时如实显示不可用，不展示部分合计。
- 银河证券 CSV 预览、逐行脱敏错误、显式确认及同一主体的批次恢复。仅保存恢复所需的批次标识，不持久化原始 CSV、token 或账户数据；刷新后重登原身份可读取服务端预览结果。导入不更新账户快照。

账户数据由用户录入、未经券商核验；没有下单、账户历史或部署管理入口。

### 测试

- `bun run test`：单元测试。
- `bun run test:e2e`：Playwright；浏览器准备命令按仓库 Playwright 配置/根验收说明执行。可控行情 fixture 是测试数据，不是实时行情或实际 IdP 证明。
  并行开发端口被占用时可设置 `A500_E2E_FIXTURE_PORT`、`A500_E2E_WEB_PORT` 与 `A500_E2E_NEXT_PORT`（默认 Web 端口加一）；测试专用网关把 UI 和 API 汇集到同一来源，不用于生产。测试产物按 Web 端口隔离。独立 Next 实例可用 `A500_NEXT_DIST_DIR` 隔离构建目录。
- 真实 Keycloak/设备授权/API 验收命令见 [信号 API 文档](../docs/signal-monitoring-api.md#compose-真实端到端验收)。
