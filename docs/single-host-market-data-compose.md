# 单机行情消费者部署

本 Compose 在一台主机运行 RabbitMQ，以及 `signals`、`audit` 两个规范化行情消费者。必须显式设置 `MARKET_DATA_MODE=tgw` 或 `replay`；模式缺失或非法时 Compose/消费者拒绝启动。两个模式分别使用 `market.events.tgw`、`market.events.replay` exchange，以及 `market.consumer.<purpose>.<mode>` durable queues，绝不跨模式混流。

## 前置条件

- 安装 Docker Engine 与 Docker Compose 插件，或 rootless Podman 与 `podman-compose`，并能拉取 Python、RabbitMQ 等所需镜像。完整应用的本地集成、HTTP/HTTPS 和生产部署见[统一 Web 部署指南](web-deployment.md)；本页独立行情消费者与回放只需基础 `compose.yaml`，无需 `compose.web.yaml`、模式文件或 UI。
- 使用 rootless Podman Compose 时，本项目配置让服务以独立容器运行，不加入 Podman pod。
- 检出项目代码；本地构建需要项目锁定的供应商 wheel，位于 `wheels/`（这些文件通过项目 `deps-v1` release 分发）。
- 基础 Compose 文件中的账户及 OIDC 必填变量在解析时也需提供，即使只选择消费者：`OIDC_ISSUER`、`OIDC_AUDIENCE`、`OIDC_JWKS_URI`、`AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST`、`AMAZINGDATA_PORT`。通过受保护环境变量或未提交的 `.env` 提供；仅启动消费者时不会因此启动账户 API 或 IdP。
- TGW collector 不由 Compose 启动。TGW SDK 运行时、许可、网络与 Python 3.14 供应商支持及自动恢复尚未完成验证；必须由已验证的外部采集器发布规范化事件。

## 网络

Docker Compose 与 Podman Compose 均使用 Compose 默认网络；容器通过 `rabbitmq` 服务名连接 RabbitMQ。Podman rootless 网络清理仍可能输出 `permission denied` 警告。

## TGW 模式

```sh
MARKET_DATA_MODE=tgw docker compose up -d --build consumer-signals consumer-audit
MARKET_DATA_MODE=tgw docker compose ps
MARKET_DATA_MODE=tgw docker compose logs -f consumer-signals consumer-audit
```

外部生产者需发布至 `market.events.tgw`。`a500-market-data-collector` 同样要求 `MARKET_DATA_MODE=tgw`；Compose 只运行消费者。

## 历史回放模式

```sh
MARKET_DATA_MODE=replay docker compose --profile replay up -d --build consumer-signals consumer-audit
MARKET_DATA_MODE=replay docker compose run --rm replay \
  a500-market-data-replay \
  --file data/market-history/563360_1m_20261102.json \
  --symbol 563360 --start-time 2026-11-02T10:00:00+08:00 --speed 10
```

历史文件以只读方式挂载到 replay 容器。只回放已完成的 1 分钟 bar；起始时间之前的 bar 作为 `warmup: true` 即时送出，之后按时间间隔除以 `--speed` 播放。默认速度 1，文件到末尾停止。事件保留历史 `event_time` 并附带回放时钟；回放消费者用它进行 30 秒新鲜度检查，不会因当前系统时间与历史日期不同而丢弃。切回 TGW 必须以 `MARKET_DATA_MODE=tgw` 启动消费者，避免消费者仍绑定 replay queue。

消费者从 `RABBITMQ_URL` 读取 AMQP 地址；Compose 内部地址为 `amqp://guest:guest@rabbitmq:5672/%2F`。若需替换 RabbitMQ 凭据或对外暴露服务，应在部署环境中配置并妥善管理凭据。

停止消费者使用 `MARKET_DATA_MODE=tgw docker compose down`（回放部署改为 `replay`）；普通 `down` 保留 RabbitMQ 命名卷，`down -v` 会删除卷及其中数据，不要用于日常停止。重新启动使用对应模式的 `up -d` 命令。完整应用的账户、Redis 和身份卷也须保护，参见[运维与数据说明](web-deployment.md#运维排障与数据)。

## 边界与验证

这是单主机 Compose 部署，不提供高可用、跨主机调度或 broker 故障切换保证。RabbitMQ durable queue 与持久化消息不等于 exactly-once；消费者语义为至少一次。离线期间逐笔事件的完整恢复不作保证。AmazingData 查询 API 不受模式开关影响。

在支持 Docker Compose 的环境运行真实端到端验证：

```sh
uv sync --frozen --no-dev
uv run --no-sync python scripts/compose-market-data-e2e.py
```

该脚本以 TGW 模式启动独立 Compose 项目（RabbitMQ、Redis 和行情消费者），通过项目的真实 `RabbitMQPublisher` 发布 schema-valid `Trade`，检查两种用途消费者均收到事件，并在清理时移除 Compose volume。每次运行使用独立 Compose 项目名和动态 RabbitMQ 主机端口，避免并行 CI 运行共享容器、数据卷或端口。需要本地容器构建走镜像时，可在运行脚本前设置 `UV_INDEX_URL`；Compose 会把该可选值传给镜像构建。GitHub Actions 未设置该值，仍使用默认 PyPI 源。

E2E 脚本为 Compose 启动设置 300 秒上限，为日志查询设置 15 秒上限，并为清理设置 60 秒上限；超时会使运行失败，而不是无限等待。
