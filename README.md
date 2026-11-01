# A500 T-Trader

用于查询标准化行情数据和订阅 tgw 推送的 Python 包。本项目不提供交易策略、下单功能，也没有可交互的命令行工具。

## 使用

- `AmazingDataProvider` 和 `MarketDataService` 提供按需快照及分钟线查询。
- `TgwPushProvider` 适配调用方已初始化的 tgw API 对象，接收快照、分钟线和逐笔成交推送。

安装前提、配置、示例、能力限制、错误处理和订阅清理方式请参阅[使用指南](docs/user-guide.md)。

## 开发与贡献

开发环境、质量检查、提交规范、持续集成和自动合并说明请参阅[开发与贡献指南](docs/contributing.md)。
