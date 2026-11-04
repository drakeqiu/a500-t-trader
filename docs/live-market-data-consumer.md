# 实时行情事件消费者

消费者只接收采集器发布的规范化 JSON 事件，不导入或调用 tgw SDK。安装项目依赖后，可独立启动一个用途实例：

```sh
 A500_MARKET_DATA_MODE=tgw \
 A500_RABBITMQ_URL=amqp://guest:guest@localhost:5672/%2F \
 a500-market-data-consumer --purpose signals --max-age-seconds 30
```

事件将以 JSON 行输出到标准输出。消费者必须显式选择 `A500_MARKET_DATA_MODE=tgw|replay`。同用途、同模式的多个进程共享 `market.consumer.<purpose>.<mode>` durable queue；TGW 与回放使用不同 exchange/queue。所有队列订阅 `snapshot.*`、`minute_bar.*` 与 `trade.*` 路由。

## 投递和事件身份

消费使用手动确认：处理成功（包括由用途新鲜度策略丢弃的过期事件）后 ack；解析、契约校验或处理失败时 nack 并 requeue。因此进程在业务处理成功但 ack 前退出时，事件可能再次投递；业务处理器必须容忍重复，语义为至少一次而非 exactly-once。契约中的 `source_event_id` 仅在 `source_event_id_quality` 为 `reliable` 时可作为可靠来源标识。当前来源未提供已确认的可靠标识，`null` 不提供去重依据，本消费者不推断或构造唯一 ID。

`event_time` 必须为带时区时间。`--max-age-seconds` 是本消费者用途自己的新鲜度阈值；回放事件以 `replay_time` 做判断，TGW 事件使用系统时钟。超过阈值的事件不交给业务处理器，但仍确认以免旧状态阻塞队列。其他用途可以选择不同阈值。队列不会按事件类型作丢弃式覆盖；逐笔事件离线期间可能无法恢复，不能视为完整成交历史。

## 与查询 API 的分离

`MarketDataService` 的请求/响应查询接口保持原样，仍由其注入的数据源提供快照、历史 K 线及成交查询。RabbitMQ 消费流不承载查询/响应，也不改变查询服务的行为。
