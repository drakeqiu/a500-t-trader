# Decouple live market-data collection from consumption

## Status

Accepted

## Context

`TgwPushProvider` currently adapts callbacks from a caller-initialized tgw API object, while live-signal consumers run in the same application process. This couples consumer availability to the provider process and leaves tgw connection and subscription recovery to the caller. The project needs independently restartable consumers and a repeatable local deployment, without making historical replay or offline tick recovery a requirement.

## Decision

Separate push collection from consumption. A collector owns the tgw connection and subscriptions, normalizes vendor callbacks into versioned project market-data events, and publishes them to RabbitMQ. Consumers depend on the normalized message contract, not the tgw SDK. Request/response queries through `MarketDataService` remain separate from the push stream.

Use independent RabbitMQ queues per consumer purpose; replicas of one purpose compete on each of that purpose's queues. For every configured symbol, snapshots and minute bars each have a durable queue bound to their exact `(event_type, symbol)` routing key with `x-max-length=1` and `x-overflow=drop-head`; this retains only the newest ready state for that key. Trades use a separate durable purpose queue with the existing competing-consumer behavior. Delivery is at least once, not exactly once, so consumers must tolerate duplicate events. Messages retain event time, and each consumer decides freshness for its use. Tick messages do not promise recovery of events missed while a consumer is offline. Include a source event identifier when reliable; otherwise any deduplication key is best-effort.

The provider manages connection and subscription lifecycle using an externally supplied connection factory and configuration. It retries disconnections with rate-limited backoff and reports connection and subscription state separately; a connection alone does not mean subscriptions or the event stream have recovered. Broker failure does not introduce a local persistent spool: while connected to tgw, the collector may retain only the latest snapshot and minute-bar state in bounded memory and publish that state after broker recovery. It does not buffer ticks for offline consumers, and a collector process restart loses this volatile state.

Use Docker Compose for a single-host deployment of RabbitMQ and consumers. Collector containerization remains conditional on validating tgw SDK runtime, licensing, and network constraints. Application roles share one image and use different startup commands. Build the image with Python 3.14 and uv using the locked dependencies (`uv sync --frozen --no-dev`); include `uv.lock` and repository-local vendor wheels in the build context before syncing.

## Consequences

The message schema, queue routing, freshness handling, duplicate tolerance, and lifecycle state become explicit seams between collection and consumers. RabbitMQ buffers transient outages but does not provide exactly-once processing or guarantee tick recovery. Before implementation, verify whether tgw exposes reliable event identifiers, connection/subscribe recovery signals, and a supported container runtime. Compose is a single-host orchestration mechanism, not a high-availability guarantee.
