# A500 T-Trader

面向量化工具使用者：按实时推送行情与 V1 策略规则生成结构化交易信号。项目不以量化研究框架或严格回测为目标。

![AmazingData 查询和已连接 tgw 推送经过各自 provider，使用 Snapshot、MinuteBar、Trade 统一行情模型](./assets/readme/market-data-flow.svg)

## 能力

- **按需查询：** `MarketDataService` 提供快照、当日分钟线和历史分钟线查询；支持 1 分钟与 5 分钟周期。
- **推送订阅：** `TgwPushProvider` 适配调用方已初始化的 tgw API，可订阅快照、分钟线和逐笔成交。
- **统一数据模型：** 查询与推送使用 `Snapshot`、`MinuteBar`、`Trade`；时间戳统一为 `Asia/Shanghai`。
- **显式能力边界：** 当前 AmazingData provider 不支持按需成交查询；数据可用性取决于连接方式、账号权限和数据源能力。
- **实时策略信号：** `live-signals` 消费注入的 push provider，以明确配置的 ETF 与已完成五分钟行情评估 V1 信号；无认证门槛，不连接券商、不验证券商账户、不下单。

## 使用

详细的安装、认证配置和依赖准备步骤见[项目依赖说明](docs/dependencies.md)与[使用指南](docs/user-guide.md)。盘前计划与历史回测是独立能力；实时信号命令、YAML 配置及 V1 参数决策见 [ADR-0009](docs/adr/0009-live-signals-and-configurable-v1-parameters.md)。异步信号监控 API 的部署边界和 HTTP/SSE 契约见[信号监控 API 指南](docs/signal-monitoring-api.md)。

项目要求 Python 3.14。所有信号及数量建议均为参考信息，不是订单、成交或收益承诺。

## 本地前后端集成

统一入口为 **<http://localhost:8080>**；使用真实 Python API、PostgreSQL、Keycloak、RabbitMQ 和 Redis，不是 mock 后端。需要 Python 3、可用的 Docker Engine + Compose 或 rootless Podman + `podman-compose`，以及 `wheels/` 中项目 `deps-v1` release 的两个锁定供应商 wheel。前端依赖在容器中使用 Bun 构建，Python 在容器中使用 uv 构建；无需宿主机安装 Bun/uv。首次构建还需能拉取 Node、Bun、Python、PostgreSQL、Keycloak、Nginx、RabbitMQ、Redis 镜像及依赖。

AmazingData 账号、密码、主机和端口使用现有未提交 `.env` 的 `AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST`、`AMAZINGDATA_PORT`；启动器也支持现有 `AD_USERNAME`、`AD_PASSWORD`、`AD_HOST`、`AD_PORT` 别名。shell 环境优先，不修改 `.env`，不输出配置值；`.env` 支持字面值、引号、注释和可选 `export`，不执行 shell 或展开变量。缺少配置时只提示字段名。账户服务必须能实际连接 AmazingData，否则启动不会被报告为成功。

持久化使用 PostgreSQL。Compose 必须设置 `A500_POSTGRES_PASSWORD`，可选的 `A500_POSTGRES_DB`、`A500_POSTGRES_USER` 和 `A500_POSTGRES_PORT` 默认分别为 `a500`、`a500` 和 `5432`。数据库端口只绑定主机回环地址，数据保存在 `postgres-data` 卷。首次使用空数据库，应用创建所需表；不会迁移旧 SQLite 数据，也不会删除旧数据库文件或卷。独立运行 CLI 或 API 时设置 `A500_DATABASE_URL`，例如 `postgresql://a500:密码@127.0.0.1:5432/a500`；CLI 的 `--database` 可显式指定 URL 并覆盖环境变量。URL 中的特殊字符需百分号编码，凭据应妥善保管。

在 Bash 中输入自行选择的本地管理员凭据（不写源码、命令历史或应用 `.env`）：

```bash
read -r -p 'Keycloak admin username: ' KEYCLOAK_ADMIN_USERNAME
read -r -s -p 'Keycloak admin password: ' KEYCLOAK_ADMIN_PASSWORD; printf '\n'
export KEYCLOAK_ADMIN_USERNAME KEYCLOAK_ADMIN_PASSWORD
read -r -s -p 'PostgreSQL password: ' A500_POSTGRES_PASSWORD; printf '\n'
export A500_POSTGRES_PASSWORD
# 必须显式选择；tgw 需外部采集器，replay 仅为历史数据回放
export A500_MARKET_DATA_MODE=tgw
python3 scripts/start-local.py
```

启动器自动选择 Docker Compose 或 `podman-compose`，也可显式设置 `A500_COMPOSE_COMMAND='podman-compose'`。它使用固定项目名 `a500-local`，组合 `compose.yaml`、`compose.keycloak.yaml`、`compose.web.yaml` 和 `compose.web-http.yaml`，构建并启动所有服务，再检查 IdP discovery、页面及同源 API 的未认证 `401`。默认就绪等待 180 秒，可加 `--timeout 300`；这不是实时行情或完整登录验收。模式、wheel、容器引擎或必要配置缺失时会明确失败。

打开 `http://localhost:8081` 管理界面，在 `a500` Realm 的 Users 中创建并启用获准用户、设置密码，然后从 `http://localhost:8080` 开始设备码授权。没有提交默认用户、管理员密码或客户端密钥，自助注册关闭。请勿以 `127.0.0.1:8080` 替代页面来源：Realm 精确允许 `http://localhost:8080` 的浏览器跨域 IdP 请求。旧 Keycloak 卷不会重新导入 Realm；已有 `a500-device-client` 需在管理界面将 Web origins 更新为该来源，不要为此删除用户卷。

浏览器与直接 API 调用均使用 `/api/v1/*`，Nginx 将请求路由到内部 Python API，前端不另行发布主机端口，也不保留旧 `/v1/*` 网关路径。API 检查的公开 issuer 为 `http://localhost:8081/realms/a500`，容器取公钥则使用 `http://keycloak:8080/realms/a500/protocol/openid-connect/certs`，不能把容器内部名当作浏览器 issuer。前端公开 IdP 配置在构建时确定，修改后需重建镜像；外部 IdP 部署见 [信号监控 API 指南](docs/signal-monitoring-api.md#compose-部署)。

仅页面内存保存 access token，不自动使用 refresh token；刷新、失效或撤销后重新设备授权。同一身份重登可恢复服务端导入批次；CSV 不持久化在浏览器，导入不更新账户快照。工作台支持 ETag 条件保存/冲突保留草稿、NAV 如实不可用、信号 SSE 游标续传和脱敏预览错误；没有下单、历史查询或部署管理功能。

```bash
python3 scripts/start-local.py ps
python3 scripts/start-local.py logs
python3 scripts/start-local.py down
```

后续命令需保留同一配置/管理员环境；`down` 不删除持久化卷（PostgreSQL、身份和 Redis 数据保留），不要加 `down -v`。HTTP gateway 默认绑定 `127.0.0.1:8080`，HTTPS 默认 `127.0.0.1:8443`；可通过 `A500_GATEWAY_HOST` 和 `A500_GATEWAY_HOST_PORT` 分别设置绑定地址和端口。Keycloak 为 `127.0.0.1:8081`。RabbitMQ 保留原配置的 `5672`，不应暴露到不受信网络。变更浏览器来源时还须同步更新 IdP 的允许来源。本地启动器使用 HTTP 和本地 Keycloak；完整生产、HTTPS、证书维护和迁移步骤见[统一 Web 部署指南](docs/web-deployment.md)。独立行情消费者无需前端或 Web 配置，见[行情 Compose 指南](docs/single-host-market-data-compose.md)。

`tgw` 模式不会自动启动 TGW 采集器；没有外部规范化行情事件时信号可显示不可用。`replay` 模式需另行显式运行下列命令，使用同一 `a500-local` 项目及配置，不能误连另一个 Compose 项目的 broker：

```bash
export A500_MARKET_DATA_MODE=replay
python3 scripts/start-local.py
python3 scripts/start-local.py replay \
  --file data/market-history/563360_1m_20261102.json \
  --symbol 563360 --start-time 2026-11-02T10:00:00+08:00 --speed 10
```

本地不连接 TGW 时，也可以使用快捷脚本启动完整服务：

```bash
./scripts/start-local-replay.sh
./scripts/start-local-replay.sh ps
./scripts/start-local-replay.sh logs
./scripts/start-local-replay.sh down
```

脚本选择 `replay` 模式，账户 API 仍使用 `.env` 中的 AmazingData 连接信息；不会自动回放数据，可使用上面的 `replay` 命令。Keycloak 首次初始化的管理员为 `admin` / `changeme`，仅供本机开发；已有 Keycloak 持久化卷不会因此重置管理员密码。应用用户仍需在 `a500` Realm 中另行创建。

可信局域网内的其他机器访问时，指定本机的局域网 IPv4 地址（不是客户端地址）：

```bash
./scripts/start-lan-replay.py 192.168.1.10
./scripts/start-lan-replay.py 192.168.1.10 ps
./scripts/start-lan-replay.py 192.168.1.10 down
```

将示例 IP 替换为本机实际地址，并从其他机器打开 `http://该IP:8080`。脚本使用独立的 `a500-lan` Compose 项目和数据卷；回放时使用 `./scripts/start-lan-replay.py 192.168.1.10 replay` 后接上述回放参数。脚本开放 Web 的 8080 和 Keycloak 的 8081，更新设备授权客户端的允许来源；RabbitMQ 仅绑定回环地址。首次启动会在 `a500` Realm 中创建应用用户 `admin` / `changeme`；已有用户和管理员的密码不会重置。HTTP 和默认密码仅适合可信局域网，禁止暴露到公网；防火墙须限制访问。使用的 AmazingData 主机也必须能从容器访问。

历史时间不会被伪装成实时行情，NAV 使用独立 AmazingData 查询及真实新鲜度约束，可能不可用。更多回放语义见 [行情 Compose 指南](docs/single-host-market-data-compose.md#历史回放模式)。确定性浏览器测试使用明确标记的行情 fixture，不能代替实时供应商或真实 IdP 证明。真实服务验收命令见 [API 验收说明](docs/signal-monitoring-api.md#compose-真实端到端验收)。

## 开发与贡献

开发环境、质量检查、提交规范和 CI 说明见[开发与贡献指南](docs/contributing.md)。
