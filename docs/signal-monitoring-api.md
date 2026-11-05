# 异步信号监控服务

## 角色与边界

`signal-api` 是只读 FastAPI 服务，通过异步 Redis 客户端读取共享当前状态并提供 SSE；它不连接 RabbitMQ、TGW、券商，也不计算信号或提交委托。`signal-calculator` 是独立的规范化行情消费者，使用相同的 `config/intraday-signals.yaml` 和 V1 评估逻辑，将状态写入 Redis。网页服务副本数量不会改变行情消费者或策略评估数量。状态及有序方向事件存储在 Redis，Redis 使用 AOF 持久化；不能用 Pub/Sub 替代 Streams。

## Compose 部署

需在已限制访问的私有网络主机部署；不得把 API 直接发布到公共互联网。仓库 Compose 不为 Redis 或 API 发布主机端口，API 仅在内部网络 `expose` 8000。反向代理应当部署在同一受控网络，唯一允许访问 API 的入口应为该代理。限制代理监听地址和来源网段（例如只允许 VPN/办公网），并在代理防火墙/访问策略中拒绝其他来源；如代理启用身份认证，认证应由代理执行。不要给 API 添加公共端口映射，也不要将 Redis、RabbitMQ 管理端口暴露给 API 用户。

```sh
A500_MARKET_DATA_MODE=tgw docker compose up -d --build redis signal-calculator signal-api
A500_MARKET_DATA_MODE=tgw docker compose ps
```

`A500_MARKET_DATA_MODE` 选择既有 RabbitMQ exchange/queue 模式；消费者应接收规范化行情事件。`signal-calculator` 使用 `A500_REDIS_URL` 写入状态；所有 API 实例和消费者必须使用同一个 Redis。修改全局 ETF 范围或 V1 参数需同时更新配置并重启计算消费者。API 默认读取镜像内随应用构建打包的 `config/intraday-signals.yaml`（Compose 从仓库配置目录复制该文件）；Redis 是状态查询、健康检查和 SSE 的必要依赖；Redis 故障时健康检查/查询返回 503，流发出不可用事件且不伪造方向。

如需以 Nginx 部署私有反向代理，可在该私有网络中的代理服务配置类似以下访问策略；`monitor.example.internal` 必须仅解析到内部地址，`10.20.0.0/16` 应替换为实际受控网段：

```nginx
server {
    listen 443 ssl;
    server_name monitor.example.internal;

    location / {
        allow 10.20.0.0/16;
        deny all;
        proxy_pass http://signal-api:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

代理必须由运维设置 TLS 证书与防火墙/监听 ACL。此配置示例不提供应用层认证；首版 API 没有应用层认证，不得通过公网 DNS、公开负载均衡器或直连主机端口开放。

## HTTP 与 SSE 契约

- `GET /healthz`：Redis 可达时返回 `200 {"status":"ok"}`，不可达时返回 `503`。
- `GET /v1/signals`：省略 `symbols` 返回全部已配置标的；可重复传递 `symbols=510300&symbols=563360`。未配置代码返回 `422`。每个条目包含 `schema_version`、标的、事件时间、策略身份、规则版本、完整参数、稳定参数指纹、状态、方向、数量和理由；尚无状态的标的也会返回带有当前策略元数据的 `UNAVAILABLE` 状态。过期、未来时间或无效事件时间均将状态标记为 `UNAVAILABLE`，清除方向/数量并标记 `stale`、`execution_eligible=false`；新鲜状态保留 V1 评估结果中的参考执行资格和数量，均不表示已下单。
- `GET /v1/signals/stream?symbols=510300&symbols=563360`：必须显式指定已配置标的；此筛选仅影响 SSE 交付范围。没有 `Last-Event-ID` 时每个标的先接收 `snapshot`。有效游标仅重放游标之后的方向事件，不发送初始快照；游标无效、超出保留边界或历史不可用时先发送 `reset`，再发送当前快照。每条方向事件的 `id` 是 Redis Stream ID，可放入 `Last-Event-ID` 恢复。方向事件单独保留；WAIT、不可用和行情/连接状态仅更新该标的 Redis 当前状态，不重复堆积历史事件。连接期间 API 定期检查当前状态以发现新鲜度过期，并只推送每标的最新状态变化。方向事件历史按 24 小时窗口修剪。

所有信号均为参考信息而非交易执行指令。浏览器必须按 `stale`/`status` 展示数据，不得把不可用状态渲染成可执行方向。
