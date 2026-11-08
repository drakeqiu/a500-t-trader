# 统一 Web 入口部署与运维

本文从仓库根目录操作。完整应用使用一个 Nginx gateway 提供 UI 和同源 `/api/v1/`，前端与 Python API 不发布宿主机端口。身份提供方（IdP）是独立服务。行情采集器不由 Compose 启动，也可只运行[独立行情消费者](single-host-market-data-compose.md)。本地集成和实际部署使用不同的 IdP，不能将开发 Keycloak 当作生产身份服务。

## 前置条件

- Linux 主机、可工作的 Docker Engine 和 Compose 插件，能拉取基础镜像并访问构建依赖；下面命令使用 `docker compose`。rootless Podman 可使用 `podman-compose`，但网络和卷权限需按其环境处理。
- 在仓库 `wheels/` 放置项目 `deps-v1` release 提供的 `AmazingData-1.1.9-cp314-none-any.whl` 和 `tgw-1.0.9.2-py3-none-any.whl`。前端镜像使用 Bun 构建，后端镜像使用 uv；无需在宿主机安装它们。构建网络需能访问依赖源，可用 `UV_INDEX_URL` 指定 Python 包索引。
- 可用的 AmazingData 查询账号、密码、主机和端口，以及从账户 API 和历史任务 worker 容器到该服务的网络。准备独立 OIDC IdP、获准用户及支持 Authorization Code + PKCE 的浏览器客户端；本地集成则使用下述开发 Keycloak。TGW 实时模式还需要外部采集器向 RabbitMQ 发布规范化行情；它的 SDK、许可和网络须自行验证。
- 凭据通过受保护的环境变量或未提交、限制访问的 `.env` 提供，不写入镜像或仓库。下例以交互式 shell 输入敏感值，避免直接写入命令历史；不要在共享终端输出环境变量或令牌。

## 本地集成：真实服务和开发 Keycloak

仅保证同一主机的浏览器访问。先设置 `AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST`、`AMAZINGDATA_PORT`（启动器也接受对应 `AD_` 别名，可放在未提交的 `.env` 中），再执行：

```bash
read -r -p 'Keycloak admin username: ' KEYCLOAK_ADMIN_USERNAME
read -r -s -p 'Keycloak admin password: ' KEYCLOAK_ADMIN_PASSWORD; printf '\n'
export KEYCLOAK_ADMIN_USERNAME KEYCLOAK_ADMIN_PASSWORD
export MARKET_DATA_MODE=tgw
python3 scripts/start-local.py
```

启动器固定 Compose 项目 `a500-local`，构建并启动基础服务、Keycloak、Web 公共定义及 HTTP 模式，并等待 discovery、UI 和未认证 API 就绪；也可指定 `--timeout 300`。`tgw` 不启动采集器，没有行情时信号可能不可用。回放的完整操作见 [README](../README.md#本地前后端集成)。

在同一台机器访问 <http://localhost:8081>，使用刚设置的管理员登录，在 `a500` Realm 中创建并启用用户、设置密码；没有默认用户，自助注册关闭。打开 <http://localhost:8080>，通过 Keycloak 登录。客户端须精确允许浏览器来源和根路径回调 URI；不要将 `localhost` 替换为 `127.0.0.1`。

本地 issuer 是 `http://localhost:8081/realms/a500`，浏览器必须能访问它；API 容器从 `http://keycloak:8080/realms/a500/protocol/openid-connect/certs` 获取公钥。前者必须与令牌 `iss` 一致，不能替换为容器内部名称。局域网启动方式见 [README](../README.md#本地前后端集成)。

```bash
python3 scripts/start-local.py ps
python3 scripts/start-local.py logs
python3 scripts/start-local.py down
```

这些命令需要保留同一配置和管理员环境；`down` 不删除持久化卷。启动器仅提供本地 HTTP/Keycloak 拓扑；HTTPS、外部 IdP 和自定义域名使用下面的 Compose 命令。

## 实际单机部署

### 配置身份和外部服务

由 IdP 管理员配置支持 Authorization Code 和 PKCE S256 的 public client，提供获准用户，使 access token 包含 API 所需 audience，并精确允许 UI 来源及根路径回调 URI。issuer 的 discovery 地址及其返回的端点必须可由用户浏览器访问；API 容器必须能访问 JWKS URI。issuer 必须与令牌的 `iss` 完全相同，audience 必须与 access token 的 `aud` 匹配。API 验证签名、issuer、audience 和有效期，账户数据按已验证的主体隔离；应用不代管用户密码或签发令牌。

以下域名和账户参数须替换成实际环境值。输入的密码不显示；也可由部署环境安全注入这些变量。

```bash
export MARKET_DATA_MODE=tgw
export OIDC_ISSUER=https://identity.example.internal/realms/a500
export OIDC_AUDIENCE=a500-api
export OIDC_JWKS_URI=https://identity.example.internal/realms/a500/protocol/openid-connect/certs
export OIDC_CLIENT_ID=a500-browser-client
read -r -p 'AmazingData username: ' AMAZINGDATA_USERNAME
read -r -s -p 'AmazingData password: ' AMAZINGDATA_PASSWORD; printf '\n'
read -r -p 'AmazingData host: ' AMAZINGDATA_HOST
read -r -p 'AmazingData port: ' AMAZINGDATA_PORT
export AMAZINGDATA_USERNAME AMAZINGDATA_PASSWORD AMAZINGDATA_HOST AMAZINGDATA_PORT
```

`OIDC_CLIENT_ID` 默认 `a500-browser-client`，并非客户端密钥。issuer 和 client ID 在前端镜像**构建时**固化；改变任一值后必须重新构建并重建前端容器。公开配置不能放入秘密。生产不叠加 `compose.keycloak.yaml`；独立 IdP 的 TLS、用户、允许来源和网络由其管理员维护。

### 选择 HTTP 或 HTTPS

每次只选择一个模式文件：`compose.web.yaml` 仅定义公共服务，本身不发布入口；额外叠加 `compose.web-http.yaml` **或** `compose.web-https.yaml`。HTTP 默认 `127.0.0.1:8080`，HTTPS 默认 `127.0.0.1:8443`。两种模式均可用 `GATEWAY_HOST` 和 `GATEWAY_HOST_PORT` 覆盖地址、端口；HTTPS 不额外开放 HTTP。例子中的 Compose 命令后续均须使用同一组文件和相同环境变量。

HTTP 只适用于可信网络或由外部设施终止 TLS、且该设施到本机的链路受保护的场景；不可信网络必须使用 HTTPS。默认回环地址不能从其他主机访问。若确需远程访问，显式设置如 `GATEWAY_HOST=0.0.0.0`，并配置主机防火墙、网络访问控制、DNS 和 IdP 允许来源。RabbitMQ 原有端口默认发布在宿主机 5672，独立于 Web 入口，必须限制对可信生产者的访问并妥善管理凭据，不要暴露到不受信网络。

HTTP 示例：

```bash
export GATEWAY_HOST=127.0.0.1 GATEWAY_HOST_PORT=8080
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-http.yaml up -d --build
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-http.yaml ps
```

HTTPS：准备与访问域名匹配、受客户端信任的 PEM 证书链和私钥，限制宿主机私钥读取权限，绝不提交私钥。将以下路径换成容器运行环境可读取的**绝对路径**；证书与私钥通过只读挂载提供给 gateway。若远程访问需同时设置适当绑定地址、防火墙和 DNS。

```bash
export TLS_CERT=/etc/a500/tls/fullchain.pem
export TLS_KEY=/etc/a500/tls/privkey.pem
export GATEWAY_HOST=127.0.0.1 GATEWAY_HOST_PORT=8443
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml up -d --build
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml ps
```

在客户端打开 `https://实际域名:8443`（若覆盖端口则使用实际端口）；证书须覆盖该域名。回环绑定适合本机浏览器，使用对应本机 DNS/hosts 和证书名称。HTTP 模式则打开实际 `http://主机:端口`。登录时跳转至独立 IdP。切换模式前，使用旧模式的文件组合执行 `down`，再以新模式启动；不要同时启动两个入口。更新 UI 来源时同步更新 IdP 的允许来源和回调 URI。

### 访问与直接调用

确认 `ps` 中服务运行、信号 API 健康，打开 UI 登录。未携带令牌的请求应返回 401，而非匿名数据；`/healthz` 只供内部探针使用，gateway 不对外提供。

```bash
# 将地址替换为实际入口；HTTPS 使用证书匹配的域名
export WEB_URL=https://a500.example.internal:8443
curl -i "$WEB_URL/api/v1/signals"
```

从 IdP 获取含正确 audience 的 **access token**（不是 ID token）；可按[设备授权命令示例](signal-monitoring-api.md#compose-真实端到端验收)操作或使用支持设备授权的 OAuth 客户端。安全地输入令牌后调用，避免泄露终端输出或 shell 历史：

```bash
read -r -s -p 'Access token: ' ACCESS_TOKEN; printf '\n'
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/signals"
curl -i -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/account-snapshot"
curl -N -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/signals/stream?symbols=563360"
# Ctrl-C 结束流
unset ACCESS_TOKEN
```

账户尚未写入快照时返回 404；SSE 初始快照及后续变化不代表已有实时行情。令牌过期或撤销后重新授权，勿记录 token。HTTPS 使用可信 CA；测试自签名证书时可通过 `curl --cacert 测试CA证书路径` 验证，不要在生产中关闭证书校验。

### 异步历史日线任务

历史任务通过独立 `jobs-api` 接受请求，`jobs-worker` 在后台处理，`history-dispatcher` 从 PostgreSQL outbox 发布通知；三个服务连接同一 PostgreSQL，worker 与 dispatcher 连接现有 RabbitMQ，不对宿主机发布端口。统一入口将 `/api/v1/jobs` 转发到内部 `/v1/jobs`。所有操作均需要与其他 API 相同的 OIDC Bearer **access token**，任务和结果按已验证的主体隔离；不是自己的任务不可访问。

升级从旧数据库轮询 worker 切换到 RabbitMQ 通知 worker 时，**先停止旧 worker，API 保持运行，再升级并启动 dispatcher 和新 worker**。不要让旧轮询 worker 与新 worker 并行，以免竞争领取任务。切换期间 `jobs-api` 可持续接受请求，任务保存在 PostgreSQL 并保持 `queued`。通过 Compose 可单独控制服务：

```bash
# 先停止旧版本的 jobs-worker；不要停止 jobs-api
docker compose stop jobs-worker
# 部署包含新 dispatcher/consumer 的版本，并启动这两个独立服务
docker compose up -d --build history-dispatcher jobs-worker
```

新版本初始化会为没有 outbox 记录的旧 `queued` 任务补建通知记录；dispatcher 随后发布存量和新任务。旧 `running` 任务由租约到期恢复机制接管。检查 `history-dispatcher` 日志中的 `History dispatch backlog pending=... ready=... oldest=...`：`pending` 是未确认 outbox 总数，`ready` 是到重试时间的数量，`oldest` 是最早积压时间。持续积压时检查 RabbitMQ 与 dispatcher 日志；同时查看 `jobs-worker` 日志及任务状态。

```bash
docker compose logs -f history-dispatcher jobs-worker
```

提交仅支持日线，日期为包含端点的 ISO 日期且都必须早于上海的今天，跨度不限。使用上文的 `WEB_URL` 并重新安全取得 `ACCESS_TOKEN`：

```bash
curl -sS -X POST "$WEB_URL/api/v1/jobs" \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"daily_history","symbol":"563360","start_date":"2024-01-01","end_date":"2024-12-31"}'
# 使用提交响应中的任务 ID
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/jobs/JOB_ID"
curl -sS -X POST -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/jobs/JOB_ID/cancel"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" "$WEB_URL/api/v1/jobs/JOB_ID/results?limit=100&offset=0"
```

提交返回 HTTP 202 和任务信息；状态为 `queued`、`running`、`succeeded`、`failed` 或 `cancelled`。`stage` 表示 `queued`、`checking_cache`、`querying`、`saving` 或 `finished`，`completed_chunks` 和 `total_chunks` 表示分片进度。结果分页包含 `job_id`、`total`、`limit`、`offset` 和 `bars`；客户端应按偏移量读取，并只使用成功任务的结果。取消为后台协作式操作，`cancel_requested` 表示请求已接受，查看状态确认最终结果。

worker 按自然年分片查询并持久化进度，可在进程中断后恢复未完成工作；任务结果固定，不因随后缓存变化而改变。只有具备可信覆盖记录的缓存可免去供应商查询；既有 CLI 共享缓存不自动视为可信。供应商返回的数据可能缺失或不完整，成功不保证每个交易日均有记录，也不保证供应商数据完整。部署者须监控 worker、PostgreSQL 和供应商连接；单机 Compose 和数据卷不提供高可用保证。

## 证书轮换

证书申请、续期和监控由部署者负责，Compose 不自动处理。证书和密钥以单个文件只读 bind mount：原位更新文件内容后可执行下面的 Nginx 重载；若通过原子替换改变宿主机文件（或更换路径），必须重建 gateway 容器以重新挂载，单纯重载或重启不足以保证读到新文件。更新后确保两者匹配、权限允许容器读取：

```bash
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml exec gateway nginx -s reload
```

原子替换文件或变更路径后：

```bash
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml up -d --force-recreate --no-deps gateway
```

如路径发生变化，先更新 `TLS_CERT`、`TLS_KEY` 再执行上述命令。重建可能短暂中断连接；客户端应重连 SSE。私钥不应进入仓库、日志或镜像。

## 迁移旧入口

1. 停止旧的独立前端及 API 端口发布，保留数据卷；选择一种 Web 模式并以新文件组合启动。前端不再单独暴露，旧 API 主机入口不再使用。
2. 将浏览器和直接客户端的业务请求从旧 `/v1/...` 改为统一入口 `/api/v1/...`，例如 `/v1/signals` 改为 `/api/v1/signals`，账户快照改为 `/api/v1/account-snapshot`。无旧路径兼容层；保留 Bearer access token、SSE `Last-Event-ID` 和客户端重连逻辑。
3. 更新 DNS、外部代理、监控和防火墙；同步 IdP 的浏览器允许来源。issuer 或客户端 ID 改变时重建前端；确认 API 容器可访问 JWKS，令牌的 issuer/audience 匹配。不要把内部健康探针当成公开监控接口。
4. 通过新入口检查 UI、设备登录、受保护信号和账户 API、SSE；确认旧端口不再监听。RabbitMQ 及外部行情生产者接口不因 Web 切换而改变。

## 运维、排障与数据

以下示例使用 HTTPS 文件组合；HTTP 部署替换为 `compose.web-http.yaml`。使用启动时相同的环境与项目名。

```bash
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml ps
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml logs -f gateway frontend signal-api account-api jobs-api jobs-worker history-dispatcher signal-calculator
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml restart gateway
docker compose -f compose.yaml -f compose.web.yaml -f compose.web-https.yaml down
```

- 配置或构建失败：确认模式、必填 OIDC/AmazingData 变量、wheel、镜像与依赖源可用；检查端口冲突。HTTPS 启动失败检查证书链、私钥匹配、挂载路径和读取权限。
- 登录失败：确认浏览器能访问 issuer/discovery、设备端点，IdP 的 Web origins 精确匹配 UI 的协议、主机和端口，用户已启用并获授权；变更公开构建配置后重建前端。
- API 返回 401：使用 access token 而非 ID token，检查过期、签名、issuer、audience 和容器到 JWKS 的连接。账户 API 启动失败检查 AmazingData 凭据和网络；信号不可用检查 Redis、calculator、行情模式与外部采集器。`ps` 健康状态及服务日志有助于定位，但不要公开凭据和令牌。
- `down` 停止并移除容器和网络，不删除命名卷。`postgres-data` 保存账户及历史任务数据库，`signal-data` 保存 Redis AOF，`rabbitmq-data` 保存 broker 数据；本地 Keycloak 的 `keycloak-data` 保存身份数据。`down -v` 会删除这些卷并造成数据丢失，不要用于日常停止或配置更新。上线前制定受限访问的备份和恢复流程；数据卷不是高可用或备份。
- 重新启动可用原文件组合执行 `up -d`；配置或镜像需要变更时使用 `up -d --build`。仅需消费者时不要启动 Web 或 IdP，按[行情消费者指南](single-host-market-data-compose.md)操作。
