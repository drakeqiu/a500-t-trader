# 使用指南

本文面向首次接入本项目行情 API 的 Python 使用者。项目提供 AmazingData 按需
查询，以及适配现成 tgw API 对象的推送订阅；它不提供交易策略、下单功能，
也没有可交互的行情查询 CLI。`python -m a500_t_trader` 目前只打印项目名称。

## 1. 准备环境

- Python 3.14（项目要求 `>=3.14,<3.15`）
- [uv](https://docs.astral.sh/uv/)
- AmazingData 账号以及服务方提供的连接地址、端口和相应行情权限
- 若使用 tgw 推送：调用方已初始化的 tgw API 对象及相应连接/权限

首次同步前，按[项目依赖说明](dependencies.md)下载所需 wheel。项目的运行时直接依赖、依赖 wheel 的获取方式及安装说明均见该文档。

在仓库根目录同步锁定的环境：

```bash
uv sync --locked
```

## 2. 配置 AmazingData

在仓库根目录创建本地 `.env` 文件：

将以下示例值替换为服务方提供的实际值：

```dotenv
AMAZINGDATA_USERNAME=your_username
AMAZINGDATA_PASSWORD=your_password
AMAZINGDATA_HOST=your.amazingdata.host
AMAZINGDATA_PORT=12345
```

账号、主机和端口的具体值及开通方式由服务方提供；不要把真实凭据提交到版本库
或写入日志。仓库的 `.gitignore` 已排除 `.env`。也可以通过进程环境变量设置
同名配置；环境变量优先于 `.env`。端口必须是 `1` 到 `65535` 的整数。

启动前，程序会确保 `$HOME/mdga_file` 存在且可写，供 SDK 使用。通过以下入口加载配置并创建真实的 AmazingData provider：

```python
from a500_t_trader import create_provider_from_env

provider = create_provider_from_env()
```

此调用执行 AmazingData internet API 模式登录。启动失败时可能抛出以下安全化异常；错误消息不会包含账号密码等配置内容：

- `ConfigurationError`：配置缺失或格式错误。
- `AuthenticationError`：认证失败。
- `PermissionDeniedError`：本地目录或远端权限不足。
- `RecoverableNetworkError`：瞬时网络错误经过有限重试后仍无法启动。
- `ProviderStartupError`：其他启动错误的基类。

先核对配置、账号权限、连接地址和 `$HOME/mdga_file` 写权限；网络错误恢复后可由调用方重新启动。不要通过输出环境变量或异常链来排查凭据问题。

## 3. 按需查询行情

`MarketDataService` 提供请求/响应式查询。它不是推送流；不传证券代码时默认使用 `563360`。证券代码接受六位代码，也可带上海/深圳交易所前后缀。

```python
from datetime import date

from a500_t_trader import create_provider_from_env
from a500_t_trader.market_data import MarketDataService

provider = create_provider_from_env()
market_data = MarketDataService(provider)

snapshot = market_data.get_snapshot()  # 默认证券代码 563360
intraday = market_data.get_intraday_bars(interval_minutes=1)
today = date.today()
history = market_data.get_history_bars("563360", today, today, interval_minutes=5)
```

可用查询：

- `get_snapshot(symbol=None)`：返回 `Snapshot` 或 `None`。
- `get_intraday_bars(symbol=None, interval_minutes=1)`：查询当天分钟线，返回 `MinuteBar` 元组。
- `get_history_bars(symbol, start_date, end_date, interval_minutes=1)`：查询所给起止日期（含首尾）的分钟线；起始日不得晚于结束日。
- 分钟线周期仅支持 `1` 或 `5` 分钟。

空结果与权限错误不同：没有快照时返回 `None`，没有分钟线时返回空元组；所请求
能力不可用或未授权时抛出 `CapabilityUnavailableError`。来源返回的分钟线缺口
会保留，不会自动补齐；尚未完成的当前分钟线标记为 `is_completed=False`。

`MarketDataService.get_trades()` 在接口上存在，但当前 AmazingData provider
不支持按需成交查询，会抛出 `CapabilityUnavailableError`。逐笔成交仅通过具备
相应能力的 tgw 推送订阅获取。

## 4. 订阅 tgw 推送

`TgwPushProvider` 需要调用方提供**已初始化的 tgw API 对象**。本项目只适配它的
订阅回调，不负责创建、登录或配置该对象；连接方法请使用你所接入 tgw 环境
提供的说明。

以下示例以迭代器接收 tick 推送。`next(subscription)` 会等待事件；实际应用可在迭代中持续处理。无论正常退出还是异常，都要关闭订阅和 provider：

```python
from a500_t_trader import TgwPushProvider

# connected_tgw 必须由调用方按其 tgw 环境完成初始化和连接。
push = TgwPushProvider(connected_tgw)
trades = push.subscribe("563360", "tick", buffer_size=256)
try:
    for trade in trades:
        print(trade)
finally:
    trades.close()
    push.close()
```

`subscribe(symbol, event_type, callback=None, *, buffer_size=256)` 支持
`snapshot`、`bar`（1 分钟线）、`tick` 三类事件。若提供 `callback=...`，事件传给
回调；不提供时返回可迭代的 `Subscription`。证券代码须为有效六位代码（可带
交易所前/后缀）。

迭代订阅的缓冲区有界：快照和分钟线缓冲区满时会丢弃旧事件、保留最新更新；
tick 不会这样合并，缓冲区溢出会终止订阅，迭代读取时抛出 `TickOverflowError`。
根据消费速度选择合适的 `buffer_size`，并处理溢出；不要把 tick 缓冲区当作可
无损无限队列。关闭 provider 会取消剩余订阅并关闭 tgw 连接。

## 5. 交易日状态

`session_status()` 返回 `(state, stale)`，可选传入待查询日期；默认查询当前上海日期：

```python
from a500_t_trader.trading_calendar import SessionState

state, stale = market_data.session_status()
if state is SessionState.UNKNOWN:
    print("交易日历不可用", stale)
else:
    print(state, stale)
```

`state` 为 `TRADING`、`CLOSED` 或 `UNKNOWN`。交易状态由交易日历数据决定，
不根据星期几推测。`stale=True` 表示日历数据不可用或过期；若没有可用的日历
数据，状态为 `UNKNOWN`。将未知状态与休市区分处理。

## 6. 统一行情模型

查询结果和 tgw 推送均使用标准模型：`Snapshot`、`MinuteBar` 和 `Trade`。金额
与价格使用人民币（CNY），成交量/数量使用股；价格和金额字段使用 `Decimal`。
时间戳是带时区的 `datetime`，统一转换为 `Asia/Shanghai`。

- `Snapshot.timestamp` 是行情时间，`received_at` 是本地接收时间，两者含义不同。
- `MinuteBar` 提供 OHLC、成交量、可选成交额及 `is_completed`；正在形成的分钟线可能变化。
- `Trade.side` 仅在来源明确提供时有意义；不要从缺失值推断买卖方向。

所有查询和订阅数据均受当前数据源、连接方式及账号权限限制。能力不可用应按异常或状态处理，不应伪装成空数据或自行推断。
