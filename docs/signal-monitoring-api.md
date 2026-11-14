# 异步信号监控服务

## 角色与边界

`signal-api` 是只读 FastAPI 服务，通过异步 Redis 客户端读取共享当前状态并提供 SSE；它不连接 RabbitMQ、TGW、券商，也不计算信号或提交委托。`signal-calculator` 是独立的规范化行情消费者，使用相同的 `config/intraday-signals.yaml` 和 V1 评估逻辑，将状态写入 Redis。状态及有序方向事件存储在 Redis，Redis 使用 AOF 持久化；不能用 Pub/Sub 替代 Streams。

## Compose 部署

Compose 要求统一 OIDC 配置 `OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_JWKS_URI`，以及账户 API 所需的 AmazingData 配置 `AD_USERNAME`、`AD_PASSWORD`、`AD_HOST`、`AD_PORT`。issuer、JWKS URI 和 `a500-api` audience 由账户 API 与信号 API 共享。调用方直接向受信任身份提供方获取 Bearer access token；浏览器使用 Authorization Code + PKCE，不提供 token broker。缺少必需值时 Compose 配置失败，不启动匿名 API。issuer 应填写 IdP 声明的规范地址。

浏览器与后端的统一本地启动见[统一 Web 部署与运维](web-deployment.md#本地集成真实服务和-rauthy)：`python3 scripts/start-local.py` 组合基础 Compose、`compose.rauthy.yaml`、`compose.web.yaml` 和 `compose.web-http.yaml`。浏览器访问 `http://localhost:8080`，Nginx 将同源 `/api/v1/*` 路由到内部 API，保留 Bearer 与 `Last-Event-ID`，代理超时为一小时。页面 token 只留内存，不自动用 refresh token；刷新或失效后重新登录，可复用 Rauthy 会话。直接 API 调用方也使用相同网关路径。

本地真实集成由仓库根目录 `scripts/start-local.py` 组合基础 Compose、`compose.rauthy.yaml`、`compose.web.yaml` 和 `compose.web-http.yaml` 启动。浏览器访问 `http://localhost:8080`；Rauthy 管理界面为 `http://localhost:8081`。启动器生成管理员凭据并 bootstrap 浏览器与设备客户端，但不会创建用户；管理员需创建获准用户。浏览器 issuer 为 `http://localhost:8081/auth/v1/`，不得将 `localhost` 改成 `127.0.0.1`。HTTP 明文传输可能被监听或篡改，凭据、授权码或令牌可能被窃取并导致账号接管。

### Rauthy 本地真实端到端验收

使用 `scripts/compose_rauthy_browser_e2e.py` 执行隔离的本地真实浏览器、设备授权及受保护 API 流程；按脚本要求使用 Podman Compose。它验证本地临时 Rauthy、API 与 gateway，不证明实际部署或生产环境。部署验收必须对真实 URL 执行，详情见[统一 Web 部署指南](web-deployment.md#rauthy-http-候选验收)。取消或未完成的范围不是通过证据，本地流程也不是远程 HTTP 安全或可用性的证据。

生产部署可连接组织管理的外部兼容 OIDC IdP。由 IdP 管理员创建支持 Authorization Code 和 PKCE S256 的 public client，关闭自助注册，不创建默认用户，并配置 access token 含 `a500-api` audience；提供获准用户，精确允许浏览器 UI 来源与根路径回调 URI。部署环境设置规范 issuer、可由 API 访问的 JWKS URI、共享 audience 和浏览器 client ID，例如：

```sh
export OIDC_ISSUER=https://identity.example.internal/realms/a500
export OIDC_AUDIENCE=a500-api
export OIDC_JWKS_URI=https://identity.example.internal/realms/a500/protocol/openid-connect/certs
export OIDC_CLIENT_ID=a500-browser-client
```

实际值必须与 IdP 配置一致，issuer 必须与令牌 `iss` 完全匹配，JWKS URI 必须可从 API 容器网络访问；`OIDC_CLIENT_ID` 默认为 `a500-browser-client`，不是 API 的认证凭据。前端公开 issuer 和 client ID 在镜像构建时写入，修改后必须重新构建前端镜像并重建容器；公开 issuer 必须可从浏览器访问。本地 Rauthy bootstrap 管理员凭据仅用于本地身份服务；生产外部 IdP 的管理员凭据应由运行环境的密钥管理/secret 注入提供，不得写入镜像、仓库、文档或应用 `.env`。Compose 使用 `.env` 时文件必须未提交且限制访问。

完整配置、HTTP/HTTPS 选择、证书、身份提供方、迁移和运维操作见[统一 Web 部署指南](web-deployment.md)。`compose.web.yaml` 仅提供公共服务定义，必须额外叠加且仅选择一个模式文件；例如外部 IdP 的 HTTP 部署：

```sh
export MARKET_DATA_MODE=tgw
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-http.yaml up -d --build
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-http.yaml ps
```

HTTP 默认绑定 `127.0.0.1:8080`，明文传输无机密性或完整性，凭据或令牌可能被窃取并导致账号接管；HTTPS 使用 `compose.web-https.yaml`，默认 `127.0.0.1:8443`，证书路径通过 `TLS_CERT` 和 `TLS_KEY` 提供。`GATEWAY_HOST` 和 `GATEWAY_HOST_PORT` 可覆盖监听地址和端口。浏览器与直接 API 调用均使用 `/api/v1/*`，旧 `/v1/*` 不再提供。账户数据库持久化在 `account-data` 卷；账户 API 需要可用的 AmazingData 凭据与服务。独立行情消费者不需 Web 配置，见[行情 Compose 指南](single-host-market-data-compose.md)。

所有信号查询和 SSE 订阅均由应用验证 Bearer access token 的签名、issuer、audience 与有效期；反向代理认证不得替代此应用层边界。网关不开放原信号 `/docs`、`/redoc` 与 `/openapi.json`，账户 schema 可通过 `/api/account/openapi.json` 访问并要求有效 Bearer token。有效共享 audience 的获准用户读取相同的行情与信号，不按 token 主体隔离。`/healthz` 仅供 Compose 内部健康探针使用，Nginx gateway 对此路径返回 `404`，不得将探针地址作为外部服务路由。未认证或无效令牌返回 `401`，并附 `WWW-Authenticate: Bearer`。

## HTTP 与 SSE 契约

- `GET /healthz`：无需 Bearer 令牌；Redis 可达时返回 `200 {"status":"ok"}`，不可达时返回 `503`。此路由不得经 gateway 暴露给用户，只由 Compose 容器内部探针访问。
- `GET /api/v1/signals`：必须提供有效共享 audience Bearer access token。省略 `symbols` 返回全部已配置标的；可重复传递 `symbols=510300&symbols=563360`。未配置代码返回 `422`。每个条目包含 `schema_version`、标的、事件时间、策略身份、规则版本、完整参数、稳定参数指纹、状态、方向、数量和理由；尚无状态的标的也会返回带有当前策略元数据的 `UNAVAILABLE` 状态。过期、未来时间或无效事件时间均将状态标记为 `UNAVAILABLE`，清除方向/数量并标记 `stale`、`execution_eligible=false`；新鲜状态保留 V1 评估结果中的参考执行资格和数量，均不表示已下单。
- `GET /api/v1/signals/stream?symbols=510300&symbols=563360`：必须提供有效共享 audience Bearer access token，并显式指定已配置标的；此筛选仅影响 SSE 交付范围。没有 `Last-Event-ID` 时每个标的先接收 `snapshot`。有效游标仅重放游标之后的方向事件，不发送初始快照；游标无效、超出保留边界或历史不可用时先发送 `reset`，再发送当前快照。每条方向事件的 `id` 是 Redis Stream ID，可放入 `Last-Event-ID` 恢复。方向事件单独保留；WAIT、不可用和行情/连接状态仅更新该标的 Redis 当前状态，不重复堆积历史事件。连接期间 API 定期检查当前状态以发现新鲜度过期，并只推送每标的最新状态变化。方向事件历史按 24 小时窗口修剪。

未认证请求以及令牌验证失败的业务请求和公开 API schema 请求返回 `401`。成功认证只证明持有获准 API access token；策略信号及行情仍是所有获准用户共享的数据，不按 `sub` 隔离。

所有信号均为参考信息而非交易执行指令。
