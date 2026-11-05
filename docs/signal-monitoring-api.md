# 异步信号监控服务

## 角色与边界

`signal-api` 是只读 FastAPI 服务，通过异步 Redis 客户端读取共享当前状态并提供 SSE；它不连接 RabbitMQ、TGW、券商，也不计算信号或提交委托。`signal-calculator` 是独立的规范化行情消费者，使用相同的 `config/intraday-signals.yaml` 和 V1 评估逻辑，将状态写入 Redis。状态及有序方向事件存储在 Redis，Redis 使用 AOF 持久化；不能用 Pub/Sub 替代 Streams。

## Compose 部署

Compose 要求统一 OIDC 配置 `A500_OIDC_ISSUER`、`A500_OIDC_AUDIENCE`、`A500_OIDC_JWKS_URI`，以及账户 API 所需的 AmazingData 配置 `AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST`、`AMAZINGDATA_PORT`。issuer、JWKS URI 和 `a500-api` audience 由账户 API 与信号 API 共享。调用方直接向受信任身份提供方获取 Bearer access token；项目不提供浏览器登录客户端或 token broker。缺少必需值时 Compose 配置失败，不启动匿名 API。issuer 应填写 IdP 声明的规范地址。
本地开发可使用 Compose 附带的 Keycloak 与 Realm 导入文件。设置启动管理员凭据（不得提交到仓库），再启动身份服务：

```sh
export KEYCLOAK_ADMIN_USERNAME='本地管理员用户名'
export KEYCLOAK_ADMIN_PASSWORD='本地管理员密码'
docker compose up -d keycloak
```

Keycloak 管理界面为 `http://127.0.0.1:8081`。Realm `a500` 从 `keycloak/realm-a500.json` 导入，设备授权 public client 为 `a500-device-client`，签发 `a500-api` audience。数据写入命名卷 `keycloak-data`，容器重启后保留；不要用 `docker compose down -v`，该命令会删除卷。Realm 文件负责初次导入；修改 Realm 配置时，应通过 Keycloak 管理界面更新现有 Realm，或在确认可丢弃数据后重建卷再导入。没有内置默认用户，Realm 自助注册关闭。管理员登录管理界面后，在 `a500` Realm 的 Users 页面显式创建并启用获准用户、设置其登录凭据；只向受信任用户发放账号，不开放注册入口。

设备授权流程中，应用先向 IdP 的设备授权端点申请设备码和用户码，展示 IdP 返回的验证 URI、用户码及轮询间隔；用户在另一设备打开验证 URI 并登录 Keycloak、确认授权。客户端按 IdP 返回的间隔轮询 token 端点，依照 OAuth 2.0 Device Authorization Grant 处理 `authorization_pending`、`slow_down`、`access_denied` 和过期结果，不得在每次 pending 时加速轮询。Keycloak discovery 地址为 `http://127.0.0.1:8081/realms/a500/.well-known/openid-configuration`；可从 discovery 读取 `device_authorization_endpoint`、`token_endpoint` 与 `jwks_uri`。本地 Keycloak 端点分别为 `/realms/a500/protocol/openid-connect/auth/device` 和 `/realms/a500/protocol/openid-connect/token`。直接使用 OAuth 客户端命令行工具时，参数对应 client ID `a500-device-client`、scope `openid`，并按该工具的设备授权实现展示验证 URI/码及轮询令牌端点；不要把用户密码发送给 A500 API。
可用 `curl` 观察完整协议交互（以下变量仅为示例配置，不含凭据）：

```sh
KEYCLOAK_BASE=http://127.0.0.1:8081
DEVICE_ENDPOINT="$KEYCLOAK_BASE/realms/a500/protocol/openid-connect/auth/device"
TOKEN_ENDPOINT="$KEYCLOAK_BASE/realms/a500/protocol/openid-connect/token"
CLIENT_ID=a500-device-client
DEVICE=$(curl --fail-with-body -sS \
  -d "client_id=$CLIENT_ID" \
  -d "scope=openid" \
  "$DEVICE_ENDPOINT")
printf '%s\n' "$DEVICE"
DEVICE_CODE=$(printf '%s' "$DEVICE" | jq -r .device_code)
USER_CODE=$(printf '%s' "$DEVICE" | jq -r .user_code)
VERIFY_URI=$(printf '%s' "$DEVICE" | jq -r .verification_uri_complete // .verification_uri)
INTERVAL=$(printf '%s' "$DEVICE" | jq -r '.interval // 5')
printf '在浏览器打开 %s 并输入用户码 %s\n' "$VERIFY_URI" "$USER_CODE"
```

用户在显示的 URI 登录并批准后，客户端使用设备码和设备授权 grant type 轮询 token endpoint，初始间隔采用响应的 `interval`（若缺省按 5 秒），每次收到 `authorization_pending` 后继续等待原间隔，收到 `slow_down` 后至少增加 5 秒；`access_denied` 或 `expired_token` 应停止并提示重新授权。示意轮询请求：

```sh
sleep "$INTERVAL"
curl --fail-with-body -sS \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
  -d "device_code=$DEVICE_CODE" \
  -d "client_id=$CLIENT_ID" \
  "$TOKEN_ENDPOINT"
```

轮询响应错误时应按 OAuth 错误码等待/停止，不要把 token 响应或设备码写入日志；成功后从 JSON 中安全读取 `access_token` 供 API 使用。示例只展示单次轮询请求，调用方需循环执行直至成功或终止错误。

API 调用必须使用 token 响应中的 `access_token`，而不是 ID token：

```sh
curl -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  https://api.example.internal/v1/signals
```

`access_token` 过期或无效时，API 返回 `401`，调用方必须停止使用该 token。调用方负责安全存储 IdP 返回的 token，并依据 IdP 支持的策略续期；若未提供/不可用 refresh token，或续期失败、授权撤销，则重新执行设备授权。不得把 token 写入不受保护的日志、源码、命令历史或共享位置。A500 只验证 IdP 签发令牌，不接收或验证用户密码、不签发令牌、不保管 refresh token，也不代表客户端续期或重新授权。

生产部署可连接组织管理的外部 Keycloak/兼容 OIDC IdP，无须部署 Compose 中的 `keycloak` 服务。由 IdP 管理员创建 public client，启用 OAuth 2.0 Device Authorization Grant，关闭自助注册，不创建默认用户，并配置 access token 含 `a500-api` audience；提供获准用户。部署环境设置规范 issuer、可由 API 访问的 JWKS URI 和共享 audience，例如：

```sh
A500_OIDC_ISSUER=https://identity.example.internal/realms/a500
A500_OIDC_AUDIENCE=a500-api
A500_OIDC_JWKS_URI=https://identity.example.internal/realms/a500/protocol/openid-connect/certs
```

实际值必须与 IdP 配置一致，issuer 必须与令牌 `iss` 完全匹配，JWKS URI 必须可从 API 容器网络访问；`a500-device-client` 的 client ID 只配置在调用方的 OAuth 设备授权客户端中，不是 A500 API 环境变量。`KEYCLOAK_ADMIN_USERNAME` 和 `KEYCLOAK_ADMIN_PASSWORD` 仅用于本地 Keycloak bootstrap 管理员；生产外部 IdP 的管理员凭据应由运行环境的密钥管理/secret 注入提供，不得写入镜像、仓库、文档或应用 `.env`。Compose 使用 `.env` 时文件必须未提交且限制访问。

在部署主机提供环境变量或未提交的 `.env` 文件后启动内部网关与 API：

```sh
A500_MARKET_DATA_MODE=tgw docker compose up -d --build redis signal-api account-api gateway
docker compose ps
```

`gateway` 在容器网络内部监听 8080，供同一受限部署网络中的 TLS 反向代理访问；不得将 Compose API 或 gateway 端口发布到公共主机接口。默认 Compose 不含 host `ports`。`deploy/oidc-gateway.conf` 将账户快照、NAV、交易导入路由送至账户 API，其余请求送至信号 API，并对 gateway 的 `/healthz` 返回 404。账户数据库持久化在 `account-data` 卷中。`signal-api` 容器健康探针仅从容器内部访问 `127.0.0.1:8000/healthz`；该路由无需用户认证，必须只用于内部探针，不得转发给外部用户。账户 API 需要实际可用的 AmazingData 凭据与服务，以启动其行情服务。
所有信号查询和 SSE 订阅均由应用验证 Bearer access token 的签名、issuer、audience 与有效期；反向代理认证不得替代此应用层边界。`/docs`、`/redoc` 与 `/openapi.json` 也要求有效 Bearer token。有效共享 audience 的获准用户读取相同的行情与信号，不按 token 主体隔离。`/healthz` 仅供 Compose 内部健康探针使用，Nginx gateway 对此路径返回 `404`，不得将探针地址作为外部服务路由。未认证或无效令牌返回 `401`，并附 `WWW-Authenticate: Bearer`。

## HTTP 与 SSE 契约

- `GET /healthz`：无需 Bearer 令牌；Redis 可达时返回 `200 {"status":"ok"}`，不可达时返回 `503`。此路由不得经 gateway 暴露给用户，只由 Compose 容器内部探针访问。
- `GET /v1/signals`：必须提供有效共享 audience Bearer access token。省略 `symbols` 返回全部已配置标的；可重复传递 `symbols=510300&symbols=563360`。未配置代码返回 `422`。每个条目包含 `schema_version`、标的、事件时间、策略身份、规则版本、完整参数、稳定参数指纹、状态、方向、数量和理由；尚无状态的标的也会返回带有当前策略元数据的 `UNAVAILABLE` 状态。过期、未来时间或无效事件时间均将状态标记为 `UNAVAILABLE`，清除方向/数量并标记 `stale`、`execution_eligible=false`；新鲜状态保留 V1 评估结果中的参考执行资格和数量，均不表示已下单。
- `GET /v1/signals/stream?symbols=510300&symbols=563360`：必须提供有效共享 audience Bearer access token，并显式指定已配置标的；此筛选仅影响 SSE 交付范围。没有 `Last-Event-ID` 时每个标的先接收 `snapshot`。有效游标仅重放游标之后的方向事件，不发送初始快照；游标无效、超出保留边界或历史不可用时先发送 `reset`，再发送当前快照。每条方向事件的 `id` 是 Redis Stream ID，可放入 `Last-Event-ID` 恢复。方向事件单独保留；WAIT、不可用和行情/连接状态仅更新该标的 Redis 当前状态，不重复堆积历史事件。连接期间 API 定期检查当前状态以发现新鲜度过期，并只推送每标的最新状态变化。方向事件历史按 24 小时窗口修剪。

未认证请求以及令牌验证失败的业务请求和 API 文档/schema 请求返回 `401`。成功认证只证明持有获准 API access token；策略信号及行情仍是所有获准用户共享的数据，不按 `sub` 隔离。
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

