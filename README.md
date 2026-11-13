# A500 T-Trader

面向量化工具使用者的实时行情与策略信号工具：通过统一行情模型接入按需查询和推送数据，并按明确配置评估 V1 实时信号。项目不是量化研究框架；信号和数量建议均为参考，不代表券商账户核验、实际成交或收益承诺。

![AmazingData 查询和已连接 tgw 推送经过各自 provider，使用 Snapshot、MinuteBar、Trade 统一行情模型](./assets/readme/market-data-flow.svg)

## 能力

- **按需行情：** 通过 `MarketDataService` 查询快照、当日分钟线和历史分钟线，支持 1 分钟与 5 分钟周期。
- **推送行情：** `TgwPushProvider` 适配调用方已初始化的 tgw API；独立采集部署将规范化行情事件发布给消费者。
- **实时信号：** `live-signals` 基于明确配置的 ETF 与已完成五分钟行情评估 V1 规则；行情缺失或过期时报告不可用状态。
- **清晰边界：** 不连接券商、不验证券商账户、不下单；数据能力取决于连接方式、账号权限和数据源。

## 开始使用

项目要求 Python 3.14。运行时依赖、供应商 wheel 获取及环境同步见[项目依赖说明](docs/dependencies.md)；行情查询、推送适配与命令行工作流见[使用指南](docs/user-guide.md)。

需要启动真实前后端集成时，见[统一 Web 部署与运维](docs/web-deployment.md#本地集成真实服务和-rauthy)。该指南包含依赖准备、Rauthy 用户与行情配置、启动、登录及停止步骤；本地入口使用真实 API 和数据服务，不是 mock 后端。

实时信号使用显式 YAML 配置，命令和规则边界见[使用指南](docs/user-guide.md#已完成-5-分钟技术分析与策略评估-api)及 [ADR-0009](docs/adr/0009-live-signals-and-configurable-v1-parameters.md)。独立行情消费者和历史回放见[单机行情消费者部署](docs/single-host-market-data-compose.md)。

## 文档

按任务浏览[文档导航](docs/README.md)，涵盖使用、开发、部署、API、策略设计、研究记录和架构决策。

## 开发与贡献

开发环境、质量检查、测试、提交规范和 CI 见[开发与贡献指南](docs/contributing.md)。
