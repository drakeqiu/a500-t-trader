# 实时行情事件消费者

消费者只接收采集器发布的规范化 JSON 事件，不导入或调用 tgw SDK。安装项目依赖后，可独立启动一个用途实例：

```sh
A500_RABBITMQ_URL=amqp://guest:guest@localhost:5672/%2F \
  a500-market-data-consumer \
  --purpose signals \
  --symbols 563360,510300 \
  --max-age-seconds 30
```

`--symbols` must list every symbol published by the collector; each consumer declares queues for its configured symbols.

Events are emitted as JSON lines to standard output. A production strategy may replace the handler with purpose-specific business logic. Each purpose owns independent durable queues: snapshots and minute bars use one bounded queue per `(purpose, symbol, event type)`, retaining only the newest queued state. Trades use a separate unbounded purpose queue. Replicas of the same purpose share each of its queues and compete for delivery; different purposes receive independent copies.

## 投递和事件身份

消费使用手动确认：处理成功（包括由用途新鲜度策略丢弃的过期事件）后 ack；解析、契约校验或处理失败时 nack 并 requeue。因此进程在业务处理成功但 ack 前退出时，事件可能再次投递；业务处理器必须容忍重复，语义为至少一次而非 exactly-once。契约中的 `source_event_id` 仅在 `source_event_id_quality` 为 `reliable` 时可作为可靠来源标识。当前来源未提供已确认的可靠标识，`null` 不提供去重依据，本消费者不推断或构造唯一 ID。

`event_time` must include a timezone. `--max-age-seconds` is freshness policy for this consumer purpose; older events are not passed to the handler but are acknowledged so stale state does not block its queue. Each snapshot/bar queue holds at most one ready message; a newer publication replaces an older queued one for the same symbol and event type without affecting other symbols, event types, purposes, or trades. Other purposes may choose different age thresholds. Trades missed while the consumer is offline are not promised to recover as a complete history.

## 与查询 API 的分离

`MarketDataService` 的请求/响应查询接口保持原样，仍由其注入的数据源提供快照、历史 K 线及成交查询。RabbitMQ 消费流不承载查询/响应，也不改变查询服务的行为。
