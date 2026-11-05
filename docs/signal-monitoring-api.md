# 异步信号监控服务

## 角色与边界

`signal-api` 是只读 FastAPI 服务，通过异步 Redis 客户端读取共享当前状态并提供 SSE；它不连接 RabbitMQ、TGW、券商，也不计算信号或提交委托。`signal-calculator` 是独立的规范化行情消费者，使用相同的 `config/intraday-signals.yaml` 和 V1 评估逻辑，将状态写入 Redis。状态及有序方向事件存储在 Redis，Redis 使用 AOF 持久化；不能用 Pub/Sub 替代 Streams。

## Compose 部署

Compose 要求统一 OIDC 配置 `A500_OIDC_ISSUER`、`A500_OIDC_AUDIENCE`、`A500_OIDC_JWKS_URI`，以及账户 API 所需的 AmazingData 配置 `AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST`、`AMAZINGDATA_PORT`。issuer、JWKS URI 和 `a500-api` audience 由账户 API 与信号 API 共享。调用方直接向受信任身份提供方获取 Bearer access token；项目不提供浏览器登录客户端或 token broker。缺少必需值时 Compose 配置失败，不启动匿名 API。issuer 应填写 IdP 声明的规范地址。

在部署主机提供环境变量或未提交的 `.env` 文件后启动内部网关与 API：

```sh
A500_MARKET_DATA_MODE=tgw docker compose up -d --build redis signal-api account-api gateway
docker compose ps
```

`gateway` 在容器网络内部监听 8080，供同一受限部署网络中的 TLS 反向代理访问；不得将 Compose API 或 gateway 端口发布到公共主机接口。默认 Compose 不含 host `ports`。`deploy/oidc-gateway.conf` 将账户快照、NAV、交易导入路由送至账户 API，其余请求送至信号 API，并对 gateway 的 `/healthz` 返回 404。账户数据库持久化在 `account-data` 卷中。`signal-api` 容器健康探针仅从容器内部访问 `127.0.0.1:8000/healthz`；该路由无需用户认证，必须只用于内部探针，不得转发给外部用户。账户 API 需要实际可用的 AmazingData 凭据与服务，以启动其行情服务。


如需以 Nginx 部署私有反向代理，可在该私有网络中的代理服务配置类似以下访问策略；`monitor.example.internal` 必须仅解析到内部地址，`10.20.0.0/16` 应替换为实际受控网段：

```nginx
server {
    listen 443 ssl;
    server_name monitor.example.internal;

    location / {
        allow 10.20.0.0/16;
        deny all;
        proxy_pass http://gateway:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

所有信号查询和 SSE 订阅均由应用验证 Bearer access token 的签名、issuer、audience 与有效期；反向代理认证不得替代此应用层边界。`/docs`、`/redoc` 与 `/openapi.json` 也要求有效 Bearer token。有效共享 audience 的获准用户读取相同的行情与信号，不按 token 主体隔离。`/healthz` 仅供 Compose 内部健康探针使用，Nginx gateway 对此路径返回 `404`，不得将探针地址作为外部服务路由。未认证或无效令牌返回 `401`，并附 `WWW-Authenticate: Bearer`。

## HTTP 与 SSE 契约

- `GET /healthz`：无需 Bearer 令牌；Redis 可达时返回 `200 {"status":"ok"}`，不可达时返回 `503`。此路由不得经 gateway 暴露给用户，只由 Compose 容器内部探针访问。
- `GET /v1/signals`：必须提供有效共享 audience Bearer access token。省略 `symbols` 返回全部已配置标的；可重复传递 `symbols=510300&symbols=563360`。未配置代码返回 `422`。每个条目包含 `schema_version`、标的、事件时间、策略身份、规则版本、完整参数、稳定参数指纹、状态、方向、数量和理由；尚无状态的标的也会返回带有当前策略元数据的 `UNAVAILABLE` 状态。过期、未来时间或无效事件时间均将状态标记为 `UNAVAILABLE`，清除方向/数量并标记 `stale`、`execution_eligible=false`；新鲜状态保留 V1 评估结果中的参考执行资格和数量，均不表示已下单。
- `GET /v1/signals/stream?symbols=510300&symbols=563360`：必须提供有效共享 audience Bearer access token，并显式指定已配置标的；此筛选仅影响 SSE 交付范围。没有 `Last-Event-ID` 时每个标的先接收 `snapshot`。有效游标仅重放游标之后的方向事件，不发送初始快照；游标无效、超出保留边界或历史不可用时先发送 `reset`，再发送当前快照。每条方向事件的 `id` 是 Redis Stream ID，可放入 `Last-Event-ID` 恢复。方向事件单独保留；WAIT、不可用和行情/连接状态仅更新该标的 Redis 当前状态，不重复堆积历史事件。连接期间 API 定期检查当前状态以发现新鲜度过期，并只推送每标的最新状态变化。方向事件历史按 24 小时窗口修剪。

未认证请求以及令牌验证失败的业务请求和 API 文档/schema 请求返回 `401`。成功认证只证明持有获准 API access token；策略信号及行情仍是所有获准用户共享的数据，不按 `sub` 隔离。

所有信号均为参考信息而非交易执行指令。

