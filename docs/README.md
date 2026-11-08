# 文档导航

按任务查找项目文档。根目录 [README](../README.md) 介绍项目能力与常用入口；生产部署和完整本地集成以[统一 Web 部署与运维](web-deployment.md)为准。

## 开始使用

- [项目依赖](dependencies.md)：获取运行时 wheel、同步依赖及构建项目包。
- [使用指南](user-guide.md)：行情查询、推送适配、实时信号和 CLI 工作流。
- [盘前计划](premarket-plan.md)：规则与账户输入、盘前计划、盘后复盘及相关 CLI。
- [开发与贡献](contributing.md)：开发环境、质量检查、测试、提交规范和 CI。

## 部署与集成

- [统一 Web 部署与运维](web-deployment.md)：本地前后端集成、单机 HTTP/HTTPS 部署、OIDC、迁移和运维。
- [单机行情消费者部署](single-host-market-data-compose.md)：独立行情消费者的 TGW 与历史回放 Compose 模式。
- [实时行情消费者](live-market-data-consumer.md)：规范化行情事件消费者及运行边界。
- [TGW 运行时验证](tgw-runtime-validation.md)：真实 TGW 环境的验证要求与步骤。

## API 与产品设计

- [信号监控 API](signal-monitoring-api.md)：信号服务边界、部署、验收及 HTTP/SSE 契约。
- [账户快照 API](account-snapshot-api.md)：账户快照、NAV 与成交文件导入 API。
- [Web 入口设计](design/compose-unified-web-entry.md)：统一网关的设计记录。
- [银河成交导入设计](design/galaxy-trade-import.md)：成交文件导入流程与边界。
- [V1 日内 ETF 做 T 策略设计](design/intraday-etf-t-strategy-v1.md)：策略规则与执行约束。
- [V1 策略源规则](strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md)：版本化规则原文。

## 研究与决策记录

- [563360 V1 回测记录](backtest-563360-v1.md)：历史数据、输入、回放及认证状态记录；研究记录不改变实时产品边界。
- [架构决策记录（ADR）](adr/)：已接受或被后续决策取代的架构决策，按编号阅读。

## 项目维护

- [Issue 跟踪约定](agents/issue-tracker.md)
- [标签分流约定](agents/triage-labels.md)
- [领域文档约定](agents/domain.md)
