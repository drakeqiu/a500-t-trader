# 单机行情消费者部署

本 Compose 部署在一台主机上运行 RabbitMQ，以及两个用途不同的规范化行情消费者（`signals`、`audit`）。两个用途分别使用 `market.consumer.<purpose>` durable queue；每个队列绑定 `snapshot.*`、`minute_bar.*`、`trade.*`，所以同一条事件会被分别路由到两个用途队列。同一用途的多个副本则竞争消费该用途队列。

## 前置条件

- 安装 Docker Engine 与 Docker Compose 插件，并能拉取 `python:3.14-slim`、RabbitMQ 镜像。
- 检出项目代码；本地构建需要项目锁定的供应商 wheel，位于 `wheels/`（这些文件通过项目 `deps-v1` release 分发）。
- 该部署不包含 TGW collector。TGW SDK 运行时、许可、网络与 Python 3.14 供应商支持及自动恢复尚未完成验证；见 [TGW 运行时验证](tgw-runtime-validation.md)。必须由已验证的外部采集器向 RabbitMQ 发布符合项目契约的规范化事件。

## 启动与维护

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f consumer-signals consumer-audit
```

消费者从 `A500_RABBITMQ_URL` 读取 AMQP 地址；Compose 内部地址为 `amqp://guest:guest@rabbitmq:5672/%2F`。若需替换 RabbitMQ 凭据或对外暴露服务，应在部署环境中配置并妥善管理凭据；默认端口仅发布到主机用于本机生产者连接。

### 从旧队列拓扑升级

旧版本按用途、事件类型和证券代码声明队列；新版本改为每个用途一个共享 durable queue。升级时先停止所有旧版消费者，再启动新版，避免同一用途的新旧消费者同时处理重复投递。RabbitMQ 不会自动删除旧队列；确认没有旧消费者且无需保留其中积压消息后，再通过 RabbitMQ 管理端清理旧队列。


```sh
docker compose pull rabbitmq
docker compose up -d --build
docker compose down
```

RabbitMQ 数据保存于 `rabbitmq-data` named volume。`docker compose down --volumes` 会删除队列和持久化数据，仅用于明确接受数据丢失的清理或 E2E 测试。

## 边界与验证

这是单主机 Compose 部署，不提供高可用、跨主机调度或 broker 故障切换保证。RabbitMQ durable queue 与持久化消息不等于 exactly-once；消费者语义为至少一次。离线期间逐笔事件的完整恢复不作保证。

在支持 Docker 的环境可运行真实端到端验证：

```sh
uv sync --frozen --no-dev
uv run --no-sync python scripts/compose-market-data-e2e.py
```

该脚本构建并启动 Compose 服务，通过项目的真实 `RabbitMQPublisher` 发布 schema-valid `Trade`，并检查两个不同用途的消费者输出均收到该事件；清理时会移除 Compose volume。GitHub Actions 的 `Compose market-data E2E` workflow 在 Docker runner 上运行同一脚本。
