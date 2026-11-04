# 使用指南

本文介绍本项目行情 API、推送适配和盘前计划 CLI。项目提供 AmazingData 按需查询、适配 tgw 推送，以及根据用户明确配置生成风险审核的做T建议。计划仅供参考，不连接券商或下单。盘前计划的完整配置样例见[盘前计划指南](premarket-plan.md)。

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

```dotenv
AMAZINGDATA_USERNAME=your_username
AMAZINGDATA_PASSWORD=your_password
AMAZINGDATA_HOST=your.amazingdata.host
AMAZINGDATA_PORT=12345
```

账号、主机和端口的具体值及开通方式由服务方提供；不要把真实凭据提交到版本库或写入日志。仓库的 `.gitignore` 已排除 `.env`。也可以通过进程环境变量设置同名配置；环境变量优先于 `.env`。端口必须是 `1` 到 `65535` 的整数。

启动前，程序会确保 `$HOME/mdga_file` 存在且可写，供 SDK 使用。通过以下入口加载配置并创建真实的 AmazingData provider：

```python
from a500_t_trader.cli.runtime import create_provider_from_env

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

from a500_t_trader.cli.runtime import create_provider_from_env
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

空结果与权限错误不同：没有快照时返回 `None`，没有分钟线时返回空元组；所请求能力不可用或未授权时抛出 `CapabilityUnavailableError`。来源返回的分钟线缺口会保留，不会自动补齐；尚未完成的当前分钟线标记为 `is_completed=False`。

`MarketDataService.get_trades()` 在接口上存在，但当前 AmazingData provider不支持按需成交查询，会抛出 `CapabilityUnavailableError`。逐笔成交仅通过具备相应能力的 tgw 推送订阅获取。

### 已完成 5 分钟技术分析与策略评估 API

> **产品目标与现状：** [ADR-0009](adr/0009-live-signals-and-configurable-v1-parameters.md) 规定推送行情驱动的实时信号、V1 参数默认值及 YAML 覆盖规则。`live-signals` CLI 订阅注入的推送 provider，仅评估已完成五分钟线；ETF 范围必须在 `config/intraday-signals.yaml` 中明确列出。实时输出仅为技术信号，不连接券商、不生成数量或订单。

`a500_t_trader.strategy.intraday_technical.analyze_intraday_technical` 使用仓库跟踪的 V1.0 源规则，对按证券隔离的已完成 `MinuteBar` 序列返回指标、趋势、分数、候选、动态网格及阻断理由。候选包括 BUY、SELL、TAKE_PROFIT、BUYBACK 和 WAIT；评估时应传入 `IntradayV1Config`、带时区的 `as_of`，以及每只证券的 `grid_context`；网格上下文至少提供 `tick_size` 和数量对应的 `round_trip_cost_rate`，可包含已持久化的网格日期、间距、挂单和已使用层级。缺少有效网格输入时不会生成交易候选。

`a500_t_trader.strategy.intraday_api.evaluate_intraday_strategy` 是公开的完整评估入口。除了市场 bars、完整 `IntradayV1Config`（含本次账户快照和逐证券规则）及带时区的 `as_of`，数量建议需提供逐证券流动性数据、持久状态库、已结订单状态和动态网格上下文。数量仍受账户、交易规则、持久状态、流动性和费用约束；网格上下文的成本率应由调用方按候选数量计算，API 不会猜测。

`paired_state` 是可选的逐证券配对映射，键为 `take_profit` / `buyback`，值必须提供基于对应真实成交及冻结网格、费用计算的 `target_price`、尚未完成的配对 `quantity` 和持久状态中的 `trade_pair_id`。TAKE_PROFIT 还要求合法可卖账户库存；BUYBACK 只允许回补该配对的未回补数量，BEAR 趋势下还受 25% 原卖出量上限约束。缺失或无效的配对目标/数量/ID 不会创造意图或数量；API 不会把账户持仓、总可卖量或历史建议猜成配对状态。信号返回配对 ID，调用方确认真实成交时应将该 ID 连同建议 ID传给 `StrategyFill`，不能生成合成配对 ID。V1 股票 ETF 的初始成熟可卖份额可通过状态库 `InitialHoldings` 显式登记，日内实际成交须经用户确认并关联配对 ID；当日新买份额不得因配对映射而绕过 T+1。

策略评估按顺序使用源定义技术分析、账户/证券/行情新鲜度校验、持久网格及订单约束，最后仅对通过前置约束的 BUY/SELL/TAKE_PROFIT/BUYBACK 候选调用 `calculate_intraday_execution`。建议通过后只记录策略建议，不记作成交；未知输入、WAIT、技术阻断或任一风险/执行约束失败时，数量为零且 `allowed` 为 false。历史回测认证不再作为实时信号或数量建议的门槛；账户/证券、行情、仓位、T+1、费用、流动性、网格及订单状态约束仍然有效。该 API 不生成订单价格、不连接券商或下单。
V1.0 的候选分数门槛与方向差门槛均取自调用方的完整策略参数（源规则基准：BUY 72、SELL 68、TAKE_PROFIT 60、BUYBACK 68，弱 BEAR 下 BUYBACK 78；方向差为 ±12），不会在执行环节退回硬编码默认值。同一交易时段内漏失已完成 5 分钟线会阻断候选，午休时段除外。通过其他限制的单笔委托数量仍受净资产 8% 上限、证券库存/仓位、可卖量、流动性及费用约束；配对意图还须受已持久化配对未完成数量约束。

`intraday-signals` 是使用默认保存的 563360 五分钟行情的历史研究回放。实时推送命令为 `python -m a500_t_trader live-signals --config config/intraday-signals.yaml`，需由应用注入已初始化的 push provider；配置明确列出 ETF 范围，可部分覆盖 V1 技术参数。配置在启动时读取，Linux 下收到 `SIGHUP` 时显式重载；配置文件不存在时参数采用规则默认值，但不会推断 ETF 范围，CLI 会拒绝空范围。实时信号只使用已完成的一分钟 bar 聚合五分钟线，历史不足或行情过期时返回无方向的 WAIT/UNAVAILABLE；不模拟成交，不给出数量，不连接券商。

### 实时网格佣金成本配置

`config/intraday-signals.yaml` 和 `config/intraday-signals-cost-example.yaml` 均使用 `live-signals` 可加载的 `costs` 配置。每只 ETF 必须显式设置 `tick_size`、`commission_rate`、`minimum_commission` 和 `reference_quantity`。当前 563360 示例配置为万0.5/边、最低0.1元/边；运行前请与本人券商账户费率及交割单核实。成本只计佣金，不加入滑点代理。

CLI 根据最新完成五分钟线收盘价 $P$ 与参考数量 $Q$ 计算：单边佣金为 `max(P × Q × commission_rate, minimum_commission)`，往返成本率为 `2 × 单边佣金 / (P × Q)`。例如价格4元、参考数量40000份时，成交额160000元，每边佣金8元，往返成本率为0.0001（1 bp）。低成交额触发最低佣金时，成本率会随价格及数量变化。

`reference_quantity` 必须是100的整数倍，只用于估算技术网格成本，不是信号建议的实际下单数量；按计划单笔规模调整。未配置某只 ETF 的成本时，历史数据达到技术分析最低要求后该证券返回 `UNAVAILABLE`，不会猜测费率。该 CLI 仍是只读建议，不连接券商或下单。配置的 `tick_size` 是显式市场规则输入；请按实际 ETF 核对。未建模真实滑点及可能另收的费用；确认万0.5是否包含交易所费用，避免漏计或重复计费。

```python
from a500_t_trader.strategy.intraday_api import evaluate_intraday_strategy

result = evaluate_intraday_strategy(
    {"563360": completed_five_minute_bars},
    as_of=aware_timestamp,
    config=v1_config,
    grid_context={
        "563360": {
            "tick_size": security_rules["tick_size"],
            "round_trip_cost_rate": quantity_specific_cost_rate,
        }
    },
    paired_state={
        "563360": {
            "take_profit": {
                "target_price": paired_buyback_target,
                "quantity": outstanding_paired_quantity,
                "trade_pair_id": take_profit_pair_id,
            },
            "buyback": {
                "target_price": paired_sell_target,
                "quantity": outstanding_paired_quantity,
                "trade_pair_id": buyback_pair_id,
            },
        }
    },
    state_store=state_store,
    pending_order_state=current_order_state,
    liquidity=liquidity_by_symbol,
```

`analyze_intraday_technical` 也可独立调用，用于只读研究；其 `risk_status == READY` 仅表示技术规则允许候选，不表示账户或执行约束允许执行。账户约束、流动性、保护底仓、可卖库存、预留现金、预算、组合仓位、风险、成交参与率和日内上限均由执行层另行检查。不得将技术候选视为订单或真实成交。

## 4. 盘前计划 CLI

按[盘前计划指南](premarket-plan.md)创建 ETF 规则文件和用户录入的账户快照，再运行：

```bash
python -m a500_t_trader premarket --config strategy.json --account account.json
```

CLI 输出 JSON 盘前做T建议及数据能力状态。规则必须由用户明确配置，不自动发现或优化策略。账户快照是用户录入数据，不是券商验证余额。必要账户、行情或日历输入缺失/过期时计划不提供可交易数量建议；数据能力不可用或未授权会明确报告。该命令不写成交、模拟成交，不连接券商，也不会下单。

如需在收盘后回看，请在生成计划时保存计划与其对应的用户录入账户快照：

```bash
python -m a500_t_trader premarket \
  --config strategy.json --account account.json --output plan-2026-10-30.json
```

监控仍使用同一 SQLite 证据库。盘后按交易日生成复盘：

```bash
python -m a500_t_trader postmarket \
  --date 2026-10-30 --plan plan-2026-10-30.json --database signals.sqlite3
```

复盘读取该日期保存的盘前建议、SQLite 触发证据，并查询该 ETF 当日可用的规范化 1 分钟历史行情；最后一根可用分钟线仅标作最新可用线，不保证是官方收盘价。输出分别列出账户快照、建议、触发、手工录入成交、Galaxy 导入成交和模拟成交。导入成交按成交时间单独展示，并标记为用户提供、未经系统验证；导入不会更新账户现金、持仓或快照。未提供或不可用的计划、触发证据、历史行情会列入 `missing_data`，不会补造。建议和触发是参考证据，不是订单或成交。

Galaxy 成交 CSV 首先以预览模式检查，不会创建或修改数据库。文件必须是 GB18030 编码且匹配支持的完整 Galaxy 导出表头。确认后才将有效新成交写入独立的导入成交记录；错误会阻止整批写入，重复成交编号不会重复导入：

```bash
python -m a500_t_trader import-fills \
  --file galaxy-fills.csv --database signals.sqlite3
python -m a500_t_trader import-fills \
  --file galaxy-fills.csv --database signals.sqlite3 --confirm
```

导入及复盘不会显示或保存文件中的股东账户、资金余额或原始行。`account-ledger` 可分别查询手工成交和导入成交。

## 5. 分钟历史回测 CLI

回测仅适用于规则文件中选定的一只 ETF。它使用历史接口返回的已完成分钟线，按配置的 `buy_below` / `sell_above` 阈值在前一根 bar 收盘后产生信号，并以紧接的下一根 bar 开盘价模拟成交。持仓遵守 T+1（当日买入不可当日卖出）和 `minimum_holding` 下限；`max_buy_cash`、初始现金/持仓、交易费和滑点均纳入约束。`--cost-bps` 为比例佣金，`--minimum-commission` 是每次成交的最低佣金，费用按 `max(成交额 × bps / 10,000, 最低佣金)` 计算；部分成交逐笔计算最低佣金。默认比例费率、最低佣金和滑点均为零。结果同时给出策略及相同初始账户的持有基线。缺失或未授权分钟历史会明确报错，不会生成替代行情或成交。

```bash
python -m a500_t_trader backtest \
  --config strategy.json --account account.json --symbol 510300 \
  --start-date 2026-10-01 --end-date 2026-10-31 --interval-minutes 1 \
  --cost-bps 0.5 --minimum-commission 0.1 --slippage-bps 5 \
  --database backtest.sqlite \
  --confirm-history-persistence-permitted
```

历史行情样例可离线查阅，无需连接 AmazingData：`data/market-history/563360_1m_20261030.json` 包含 2026-10-30 全天 242 条 1 分钟记录（上海时间 09:30 至 15:00，含午间休市区间）；`data/market-history/563360_1m_20261102.json` 是 2026-11-02 截至 11:28 的 119 条部分交易日记录。

仅当数据服务条款允许本地保存分钟历史时，才提供最后的确认选项；未确认时程序在查询和写入之前退出。SQLite 共用历史按证券/周期/时间复用；每次回测另存内容寻址的不可变 bar 快照、初始参数、实现版本、费用/滑点假设、模拟成交、策略结果和持有基线。返回的 `run_id` 可用于复核该次记录及其准确输入：

```bash
python -m a500_t_trader backtest-show --database backtest.sqlite --run-id 1
python -m a500_t_trader history-cleanup --database backtest.sqlite
```

`backtest-show --latest` 可显示最新运行；`history-cleanup` 只清除可复用的共用历史，不删除回测引用的快照或记录。模拟成交明确标记为非实际成交。本工具不连接券商、不下单，也不提供投资建议。

策略信号 API 可直接调用 `a500_t_trader.backtest.run_intraday_backtest` 进行 bar 模拟：传入已完成 bars、`BarSignal` 序列或实现 `StrategySignalProvider` 的信号提供器、账户快照和执行参数。模拟仅在后续 bar 开盘尝试成交，并记录部分成交、撤单、预留现金、费用/滑点、T+1 库存、输入 bars 快照及其 SHA-256 标识、策略参数和假设；同时提供相同初始现金/持仓的持有基线和两者收益率。

`TechnicalAnalysisSignalProvider` 将 `analyze_intraday_technical` 的因果 BUY/SELL 候选转换为模拟信号：每次分析只看当根及此前已完成 bars，可传入 V1 配置和固定/逐 bar 网格上下文。它只转换 `risk_status == READY` 的候选；必须另行提供 `CandidateQuantityResolver`，由其在账户、持仓、可卖数量、资金、流动性和策略限制等执行信息齐备时返回 100 份整数倍数量，否则返回 `None` 不发信号。网格状态需要跨 bar 更新时，应实现 `TechnicalGridContextProvider`。分析器的技术候选本身不代表账户已授权执行；缺失的外部数据仍须由解析器阻断。本 API 不会推测或补造缺失的信号、数量或账户资格。

回测认证由 `a500_t_trader.backtest.certify_intraday_backtest` 按显式认证配置评估。必须提供扣费后正收益、正的配对交易平均净利、总 PF 至少 1.25、样本内/外 PF 均大于 1、最大回撤、样本量、成本/滑点乘数 1.5 下的收益和 PF、每个已配置邻域扰动的收益/PF，以及每个 walk-forward 切分的收益/PF。最低收益/净利、最大回撤预算、最低样本数、扰动参数范围、walk-forward 切分数和最低通过切分数都必须由用户配置；缺失配置或指标会返回未认证和阻断原因，不存在默认风险档位。

认证指纹涵盖规则源 SHA-256/规则版本/全部参数、实现版本、行情快照 SHA-256、完整成本模型和完整认证配置。每次授权前消费方必须调用 `certification_is_current` 并将当前输入提供给它；输入任何变化都会使旧认证失效。认证仅表示这些历史输入达到所配置条件，不保证未来盈利，也不构成下单授权或投资建议。

针对 563360、现金 210,000 元和持仓 400,700 份的 V1.0 回测准备记录及缺失输入清单，见[回测准备记录](backtest-563360-v1.md)。该记录不表示已执行回测。

## 6. 订阅 tgw 推送

`TgwPushProvider` 需要调用方提供**已初始化的 tgw API 对象**。本项目只适配它的订阅回调，不负责创建、登录或配置该对象；连接方法请使用你所接入 tgw 环境提供的说明。

以下示例以迭代器接收 tick 推送。`next(subscription)` 会等待事件；实际应用可在迭代中持续处理。无论正常退出还是异常，都要关闭订阅和 provider：

```python
from a500_t_trader.market_data.providers.tgw_push import TgwPushProvider

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

`subscribe(symbol, event_type, callback=None, *, buffer_size=256)` 支持 `snapshot`、`bar`（1 分钟线）、`tick` 三类事件。若提供 `callback=...`，事件传给回调；不提供时返回可迭代的 `Subscription`。证券代码须为有效六位代码（可带交易所前/后缀）。

迭代订阅的缓冲区有界：快照和分钟线缓冲区满时会丢弃旧事件、保留最新更新；tick 不会这样合并，缓冲区溢出会终止订阅，迭代读取时抛出 `TickOverflowError`。根据消费速度选择合适的 `buffer_size`，并处理溢出；不要把 tick 缓冲区当作可无损无限队列。关闭 provider 会取消剩余订阅并关闭 tgw 连接。

## 7. 交易日状态

`session_status()` 返回 `(state, stale)`，可选传入待查询日期；默认查询当前上海日期：

```python
from a500_t_trader.market_data.trading_calendar import SessionState

state, stale = market_data.session_status()
if state is SessionState.UNKNOWN:
    print("交易日历不可用", stale)
else:
    print(state, stale)
```

`state` 为 `TRADING`、`CLOSED` 或 `UNKNOWN`。交易状态由交易日历数据决定，不根据星期几推测。`stale=True` 表示日历数据不可用或过期；若没有可用的日历数据，状态为 `UNKNOWN`。将未知状态与休市区分处理。

## 8. 统一行情模型

查询结果和 tgw 推送均使用标准模型：`Snapshot`、`MinuteBar` 和 `Trade`。金额与价格使用人民币（CNY），成交量/数量使用股；价格和金额字段使用 `Decimal`。时间戳是带时区的 `datetime`，统一转换为 `Asia/Shanghai`。

- `Snapshot.timestamp` 是行情时间，`received_at` 是本地接收时间，两者含义不同。
- `MinuteBar` 提供 OHLC、成交量、可选成交额及 `is_completed`；正在形成的分钟线可能变化。
- `Trade.side` 仅在来源明确提供时有意义；不要从缺失值推断买卖方向。

所有查询和订阅数据均受当前数据源、连接方式及账号权限限制。能力不可用应按异常或状态处理，不应伪装成空数据或自行推断。

## Intraday strategy CLI

`intraday-strategy --config <v1.json> --bars <bars.json> --as-of <timestamp> [--output <result.json>]`
loads the explicit V1 configuration and a JSON array of minute-bar objects
(`symbol`, `timestamp`, `interval_minutes`, `open`, `high`, `low`, `close`,
`volume`; optional `amount` and `is_completed`) and calls the public strategy
API. Standard output and `--output` contain the same structured JSON result.
The `live-signals` command subscribes to an injected push provider and emits
only completed five-minute advisory evaluations. Its YAML file explicitly
scopes ETFs and may override V1 technical parameters; missing config uses
defaults but does not infer an ETF universe. Certification is not a live-signal
or sizing gate. Execution inputs and constraints remain fail-closed. Neither
CLI connects to a broker or submits orders.
