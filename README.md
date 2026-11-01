# A500 T-Trader

面向 Python 调用方的行情数据 API：通过 AmazingData 按需查询快照和分钟线，或将调用方已连接的 tgw 推送适配为统一行情模型。

![AmazingData 查询和已连接 tgw 推送经过各自 provider，使用 Snapshot、MinuteBar、Trade 统一行情模型](./assets/readme/market-data-flow.svg)

## 能力

- **按需查询：** `MarketDataService` 提供快照、当日分钟线和历史分钟线查询；支持 1 分钟与 5 分钟周期。
- **推送订阅：** `TgwPushProvider` 适配调用方已初始化的 tgw API，可订阅快照、分钟线和逐笔成交。
- **统一数据模型：** 查询与推送使用 `Snapshot`、`MinuteBar`、`Trade`；时间戳统一为 `Asia/Shanghai`。
- **显式能力边界：** 当前 AmazingData provider 不支持按需成交查询；数据可用性取决于连接方式、账号权限和数据源能力。

## 使用

本项目要求 Python 3.14。安装前需获取 Release 中的 `AmazingData` 与 `tgw` wheel，并同步仓库环境；首次设置步骤见[项目依赖说明](docs/dependencies.md)。

查询、认证配置、推送订阅示例及资源清理方式见[使用指南](docs/user-guide.md)。项目不提供交易策略、下单功能或交互式行情查询 CLI；`python -m a500_t_trader` 仅打印项目名称。

## 开发与贡献

开发环境、质量检查、提交规范和 CI 说明见[开发与贡献指南](docs/contributing.md)。
