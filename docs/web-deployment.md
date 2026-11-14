# 统一 Web 入口部署与运维

本文从仓库根目录操作。完整应用使用一个 Nginx gateway 提供 UI 和同源 `/api/v1/`，前端与 Python API 不发布宿主机端口。身份提供方（IdP）是独立服务。行情采集器不由 Compose 启动，也可只运行[独立行情消费者](single-host-market-data-compose.md)。本地真实集成由 `compose.rauthy.yaml` 提供 Rauthy；实际部署可使用外部兼容 OIDC IdP。

## 前置条件

- Linux 主机、可工作的 Docker Engine 和 Compose 插件，能拉取基础镜像并访问构建依赖；下面命令使用 `docker compose`。rootless Podman 可使用 `podman-compose`，但网络和卷权限需按其环境处理。
- 本地完整启动脚本在构建服务镜像前运行 `uv build --wheel`；直接运行 Compose 构建时，先在仓库根目录执行 `uv build --wheel`。镜像内使用 `uv pip install` 安装项目 wheel 和依赖；PyPI/镜像源仍需可访问，供应商依赖 wheel 则从 `wheels/` 提供。不再使用 uv 镜像缓存。
- 可用的 AmazingData 查询账号、密码、主机和端口，以及从账户 API 和历史任务 worker 容器到该服务的网络。准备独立 OIDC IdP、获准用户及支持 Authorization Code + PKCE 的浏览器客户端；本地集成使用下述 Rauthy。TGW 实时模式还需要外部采集器向 RabbitMQ 发布规范化行情；它的 SDK、许可和网络须自行验证。
- 凭据通过受保护的环境变量或未提交、限制访问的 `.env` 提供，不写入镜像或仓库。下例以交互式 shell 输入敏感值，避免直接写入命令历史；不要在共享终端输出环境变量或令牌。

## 本地集成：真实服务和 Rauthy

本地集成启动器和 Compose 服务使用 `AD_USERNAME`、`AD_PASSWORD`、`AD_HOST`、`AD_PORT` 配置行情服务；这些连接信息从启动器所在项目根目录的 `.env` 读取，并优先于同名进程环境变量。其他配置仍由进程环境变量覆盖 `.env`。可将行情配置放在未提交的 `.env` 中。启动本地集成：

```bash
export MARKET_DATA_MODE=tgw
python3 scripts/start-local.py
```

启动器默认使用 Compose 项目 `a500-local`，也可用 `COMPOSE_PROJECT_NAME` 显式指定独立项目。它叠加 `compose.rauthy.yaml`、Web 公共定义及 HTTP 模式，构建并启动真实服务及 Rauthy，并等待 discovery、UI 和未认证 API 就绪；也会启动 `jobs-api`、`jobs-worker` 和 `history-dispatcher`，以支持历史行情任务。也可指定 `--timeout 300`。本地启动器默认开启历史行情持久化；启动前仍须确认 AmazingData 条款允许持久化，如需关闭，可在 `.env` 或环境变量中设置 `HISTORY_PERSISTENCE_PERMITTED=false`。`tgw` 不启动采集器，没有行情时信号可能不可用。回放操作见[单机行情消费者部署](single-host-market-data-compose.md#历史回放模式)；启动器内的 replay 参数见仓库根目录 `scripts/start-local.py replay --help`。

启动器在 `.rauthy-bootstrap/` 中以权限受限的文件生成并持久保存 Hiqlite 密钥、初始管理员密码及客户端更新 API key；该目录不可提交或公开。API key secret 使用至少 64 个字符的随机值（Rauthy v0.37.2 的最低要求），检测到较短的旧值时会自动轮换并同步 bootstrap 文件。首次启动后的客户端 audience 由 Rauthy 客户端 API 设置并读回为 `a500-api`，不依赖不受 bootstrap schema 支持的 `default_aud` 字段。

打开 <http://localhost:8081> 进入 Rauthy 管理界面；启动器已 bootstrap 浏览器和设备客户端，但不会创建用户。管理员须创建并启用获准用户，再打开 <http://localhost:8080> 登录。不要将 `localhost` 替换为 `127.0.0.1`。

本地 issuer 是 `http://localhost:8081/auth/v1/`，浏览器必须能访问它；API 从容器网络 Rauthy JWKS 获取公钥。issuer 必须与令牌 `iss` 一致。局域网启动方式见本地[回放与局域网访问](#本地回放与局域网访问)。

```bash
python3 scripts/start-local.py ps
python3 scripts/start-local.py logs
python3 scripts/start-local.py down
```

这些命令需要保留同一配置和管理员环境；`down` 不删除持久化卷。启动器提供本地 HTTP/Rauthy 拓扑；HTTPS、外部 IdP 和自定义域名使用下面的 Compose 命令。

### 本地回放与局域网访问

不连接 TGW 时，可用快捷脚本启动完整本地服务；脚本选择 `replay` 模式，但不会自动回放行情：

```bash
./scripts/start-local-replay.sh
./scripts/start-local-replay.sh ps
./scripts/start-local-replay.sh logs
./scripts/start-local-replay.sh down
```

启动后按[单机行情消费者部署](single-host-market-data-compose.md#历史回放模式)准备回放数据；完整本地服务的回放命令见 `python3 scripts/start-local.py replay --help`。账户 API 仍需可用的 AmazingData 配置。

可信局域网内访问时，使用本机的局域网 IPv4 地址启动独立项目：

```bash
./scripts/start-lan-replay.py 192.168.1.10
./scripts/start-lan-replay.py 192.168.1.10 ps
./scripts/start-lan-replay.py 192.168.1.10 down
```

将示例地址替换为本机实际地址，客户端访问 `http://该IP:8080`。该脚本使用独立的 `a500-lan` 项目和数据卷，并开放 Web 与 Rauthy 端口；同步配置浏览器客户端允许来源和回调 URI。本段的默认凭据与网络约束仅适用于此本地开发 helper，不构成 Rauthy HTTP 候选的验收门槛。`down` 不删除持久化卷；切勿用 `down -v` 作为日常停止方式。

LAN 回放模式只将实时行情链路设为 `replay`；历史任务由 `jobs-worker` 直接使用 `.env` 中的 `AD_*` 配置，不依赖盘中行情。启动器也会启动 `jobs-api`、`jobs-worker` 和 `history-dispatcher`。确认 AmazingData 条款允许持久化后，直接运行：

```bash
./scripts/start-lan-replay.py 192.168.1.10
```

本地启动器默认开启持久化；如需关闭，可在 `.env` 或环境变量中设置 `HISTORY_PERSISTENCE_PERMITTED=false`。

## 实际单机部署

### 配置身份和外部服务

由 IdP 管理员配置支持 Authorization Code + PKCE S256 的 public client 和独立设备授权 public client，提供获准用户；两种流程签发的 access token 都须包含 API 所需 audience。issuer 的 discovery 地址及授权／令牌端点必须由实际浏览器和设备客户端访问；API 容器必须能访问 JWKS URI。issuer 必须与令牌的 `iss` 完全一致。API 验证签名、issuer、audience、令牌用途和有效期，账户数据按已验证的主体隔离；应用不代管用户密码或签发令牌。

以下域名和账户参数须替换成实际环境值。输入的密码不显示；也可由部署环境安全注入这些变量。

```bash
export MARKET_DATA_MODE=tgw
export OIDC_ISSUER=https://identity.example.internal/realms/a500
export OIDC_AUDIENCE=a500-api
export OIDC_JWKS_URI=https://identity.example.internal/realms/a500/protocol/openid-connect/certs
export OIDC_CLIENT_ID=a500-browser-client
read -r -p 'AmazingData username: ' AD_USERNAME
read -r -s -p 'AmazingData password: ' AD_PASSWORD; printf '\n'
read -r -p 'AmazingData host: ' AD_HOST
read -r -p 'AmazingData port: ' AD_PORT
export AD_USERNAME AD_PASSWORD AD_HOST AD_PORT
```

`OIDC_CLIENT_ID` 默认 `a500-browser-client`，并非客户端密钥。issuer 和 client ID 在前端镜像**构建时**固化；改变任一值后必须重新构建并重建前端容器。公开配置不能放入秘密。本地使用 Rauthy；外部部署可使用兼容 OIDC IdP。HTTP 明文传输可能导致凭据、授权码、会话或令牌被监听、篡改、窃取并造成账号接管。不得将 HTTP 描述为提供 HTTPS 保护。


### 选择 HTTP 或 HTTPS

每次只选择一个模式文件：`compose.web.yaml` 仅定义公共服务，本身不发布入口；额外叠加 `compose.web-http.yaml` **或** `compose.web-https.yaml`。HTTP 默认 `127.0.0.1:8080`，HTTPS 默认 `127.0.0.1:8443`。两种模式均可用 `GATEWAY_HOST` 和 `GATEWAY_HOST_PORT` 覆盖地址、端口；HTTPS 不额外开放 HTTP。例子中的 Compose 命令后续均须使用同一组文件和相同环境变量。

HTTP 默认回环绑定不能从其他主机访问；按实际浏览器、设备客户端和后端的访问路径设置绑定地址、身份 issuer、网关及 DNS/hosts。该候选允许实际路径使用 HTTP，不要求配置 TLS、受控 LAN/VPN 或限制公网访问。HTTP 不提供传输机密性或完整性，攻击者可能监听或篡改登录与令牌流量、窃取凭据和令牌并接管账号；HTTPS 能降低这些风险，但不是候选验收条件。RabbitMQ 原有端口默认发布在宿主机 5672，独立于 Web 入口。

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

### Rauthy HTTP 候选验收

仓库的 `scripts/compose_rauthy_browser_e2e.py` 使用隔离 Compose 项目验收临时 Rauthy、API、gateway、本地真实浏览器和设备授权流程。它足以验证本机隔离候选，无需单独部署真实生产主机、独立环境或部署方真实身份 URL。候选隔离表示不接管现行环境，不是网络访问限制。

验收仍须完成真实浏览器 PKCE S256、独立设备授权流程，并使用两者取得的真实 access token 经 gateway 调用 Bearer API；确认签名、issuer、audience、令牌用途及有效期校验有效，并验证两个主体只能访问各自账户数据。浏览器因安全上下文或浏览器限制无法完成登录时，须如实记录为未通过。

同一隔离候选使用独立持久卷完成验收后，执行普通停止／启动（不删除卷），重新用原用户登录并确认客户端／身份配置及重启前的账户测试数据仍在、主体保持一致。不得用 `down -v`；测试清理只能移除本轮创建的资源，不能删除其他项目卷。记录实际 URL、服务版本、流程/API/隔离/重启结果及清理动作；证据不得包含密码、授权码、令牌或密钥。所有验收同时明确记录 HTTP 的明文拦截、篡改、凭据／令牌盗窃及账号接管风险。取消或未完成的范围不是通过证据。


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
```

提交返回 HTTP 202 和任务信息；状态为 `queued`、`running`、`succeeded`、`failed` 或 `cancelled`。`stage` 表示 `queued`、`checking_cache`、`querying`、`saving` 或 `finished`，`completed_chunks` 和 `total_chunks` 表示分片进度。取消为后台协作式操作，`cancel_requested` 表示请求已接受，查看状态确认最终结果。任务成功时将暂存行情及其查询覆盖一并发布到共享历史存储；任务状态不提供行情结果快照。

worker 按自然年分片查询并持久化进度，可在进程中断后恢复未完成工作。同一 `jobs-worker` 进程惰性创建并复用一个供应商 Provider，多个任务共享该进程中的登录状态；供应商调用在 worker 内串行执行。重启 worker 会重新创建 Provider 并登录。只有具备可信覆盖记录的缓存可免去供应商查询；既有 CLI 共享缓存不自动视为可信。供应商返回的数据可能缺失或不完整，成功不保证每个交易日均有记录，也不保证供应商数据完整。部署者须监控 worker、PostgreSQL 和供应商连接；单机 Compose 和数据卷不提供高可用保证。

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
- `down` 停止并移除容器和网络，不删除命名卷。`postgres-data` 保存账户及历史任务数据库，`signal-data` 保存 Redis AOF，`rabbitmq-data` 保存 broker 数据；本地 Rauthy 的 `rauthy-data` 保存身份数据。`down -v` 会删除这些卷并造成数据丢失，不要用于日常停止或配置更新。上线前制定受限访问的备份和恢复流程；数据卷不是高可用或备份。
- 重新启动可用原文件组合执行 `up -d`；配置或镜像需要变更时使用 `up -d --build`。仅需消费者时不要启动 Web 或 IdP，按[行情消费者指南](single-host-market-data-compose.md)操作。
