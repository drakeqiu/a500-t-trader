# 盘前计划

`python -m a500_t_trader premarket` 根据本地 ETF/规则配置、用户录入账户快照、规范化行情快照和上海交易日历生成 JSON 盘前计划。计划只提供做T建议，不连接券商、不验证账户或成交，也不会下单。策略规则由用户明确配置，程序不自动发现或优化策略。

## ETF 和规则配置

创建本地 JSON 文件（例如 `strategy.json`）：

```json
{
  "etfs": [
    {
      "symbol": "510300",
      "buy_below": "3.85",
      "sell_above": "4.10",
      "max_buy_cash": "4000",
      "minimum_holding": 100
    }
  ]
}
```

`buy_below` 和 `sell_above` 是明确的价格触发条件（CNY/份）；至少配置一项。`max_buy_cash` 限制单条买入建议占用的现金；`minimum_holding` 是卖出后保留的最低份额。数量按 100 份一手向下取整。ETF 列表及规则只读自此本地配置，不会自动筛选或改变规则。可配置多个唯一 ETF。

## V1 策略配置核验

为确认 RSI 修订后的 V1 日内 ETF 做 T 规则及显式输入身份，可运行 `python -m a500_t_trader intraday-config --config <配置文件>`。配置必须使用 `schema_version`=`2.0`（旧格式明确拒绝）、`strategy_id`=`intraday-etf-t-v1-rsi`、`rule_version`=`V1.0`，并显式提供 `implementation_version`、`rule_source`、`rule_source_sha256`、完整 `parameters`、`certification` 门槛、带来源和时点的 `fees`、带时点的 `account` 输入，以及逐证券 `securities` 交易规则。参数项、认证阈值、费率、账户值及证券规则均须由操作者填写；程序不会补入示例、典型值或默认假设。缺失项会以字段路径报错；策略标识、源路径或源哈希与受支持的 RSI 规则不符时拒绝载入。

受支持的 V1.0 RSI 规则原文为 `docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md`（精确字节 SHA-256：`ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4`）。这些值标识当前规则修订；仓库规则文本仅供审计，不会作为隐式运行配置，也不会据此推断或补入默认值。

核验成功时输出 schema、策略标识、规则版本、实现版本、规则源标识与 SHA-256、以及规范化完整配置的 SHA-256 指纹。显式配置值变化会改变指纹；仅 JSON 空白或键顺序变化不会改变指纹。记录输出即可追溯所用 RSI 规则来源和输入。本命令只验证与识别配置，不运行策略或认证回测。

## 账户快照

手动创建一个账户快照 JSON（例如 `account.json`）：

```json
{
  "as_of": "2026-10-30T08:25:00+08:00",
  "holdings": { "510300": 1200 },
  "cost_basis": { "510300": "3.70" },
  "sellable": { "510300": 800 },
  "available_cash": "5000"
}
```

`as_of` 必须带时区。持仓、成本基础、可卖数量及可用资金均是用户录入数据，不是券商验证或实时余额。账户快照不会被计划覆盖；计划也不会写入成交记录。

## 生成计划

在配置了 AmazingData 行情访问权限的环境中：

```bash
python -m a500_t_trader premarket --config strategy.json --account account.json
```

输出为 JSON，每个 ETF 包含方向、触发条件、理由、数量和风险/失效说明。信号强度只有规则显式定义评分时才表示策略评分；本版本这些价格触发规则不定义评分，因此信号强度为空。策略评分不代表收益概率，也不代表风险审核结论。

风险审核先于数量生成：持仓/账户输入或行情超过 30 分钟、行情缺失/未授权、交易日历不可用/过期、或当前日期非交易日时，不给出可交易数量；风险否决会把方向设为 `none`。JSON 中 `capability_status` 会报告行情缺失/过期。运行时不能访问所需能力时，CLI 会在标准错误中指出能力不可用或未授权并返回非零状态。行情和日历状态只来自现有 provider 和交易日历服务；不会根据工作日推测交易日。

本 CLI 当前只生成建议。建议、用户录入账户快照、手工成交记录和模拟成交是不同数据；该命令不创建后两种记录，不执行交易。

## 盘后复盘

如需复盘，请在计划生成时将计划和对应账户快照另存为本地 JSON：

```bash
python -m a500_t_trader premarket \
  --config strategy.json --account account.json --output plan-2026-10-30.json
```

使用 `monitor` 时将 `--database` 指向需复用的同一触发证据库。收盘后运行：

```bash
python -m a500_t_trader postmarket \
  --date 2026-10-30 --plan plan-2026-10-30.json --database signals.sqlite3
```

`--date` 必须与保存计划的交易日相同。JSON 复盘单独呈现盘前建议、用户录入且非券商验证的账户快照、监控触发证据、手工录入成交和模拟成交；建议不会转成订单。手工成交可通过 `manual-fill` 写入同一 SQLite 证据库，账户快照可使用 `account-snapshot` 持久化，且复盘会读取当天最新快照及手工成交；未记录的类别显示为 `not recorded`，不推断已执行。按计划 ETF 查询当日历史 1 分钟线；若数据源没有该能力、没有可用数据、没有对应触发库/记录或保存计划不匹配，`missing_data` 会说明缺项。最后一根历史分钟线不保证是官方收盘价。
账户快照及手工成交的独立录入命令：

```bash
python -m a500_t_trader account-snapshot --account account.json --database signals.sqlite3
python -m a500_t_trader manual-fill --timestamp 2026-10-30T10:02:00+08:00 \
  --symbol 510300 --side buy --quantity 100 --price 3.80 --database signals.sqlite3
python -m a500_t_trader account-ledger --database signals.sqlite3
```

## 用户启动盘中监控

监控只在用户前台启动的进程中运行。提供本地账户快照、规则配置及用于保存触发证据的 SQLite 路径：

```bash
python -m a500_t_trader monitor \
  --config strategy.json --account account.json --database signals.sqlite3
```

监控默认每 5 秒查询一次各规则 ETF 的规范化 1 分钟线，按交易日历状态决定是否评估。每轮仅检查最新的已完成分钟线和最新的未完成分钟线，不回放当天较早的历史触发。`buy_below` 在分钟线最低价达到或低于阈值时触发；`sell_above` 在最高价达到或高于阈值时触发。监控会标示未完成分钟线产生的提醒为暂定，并与已完成分钟线分别记录。提醒包含方向、条件、行情时间和风险/失效信息，并明确标注不是订单；不计算或显示可交易数量，也不执行交易。

每次触发会写入 SQLite 的 `trigger_evidence` 表，保存规则条件、规范化分钟线 OHLC/成交量/成交额、来源、交易日状态、评估时间、完成状态、风险说明及用户输入的账户快照时间和来源。证据保留账户快照时间以便区分账户输入与行情信号；不会将账户快照、手工录入成交或模拟成交合并为建议或触发记录。同一证券、方向、周期和分钟时间的同一触发不会重复保存。

按 `Ctrl-C` 停止当前监控进程；进程退出后不再监控。用 `--once` 只运行一轮后退出，可用于手动检查数据能力、提醒及证据持久化；用 `--poll-seconds N` 指定正数轮询间隔。交易日历未知/过期、分钟线能力不可用/未授权或无分钟线时会明确报告，且不会伪造行情或数量建议。

## 日线历史与指标

`daily-history` 查询标准化日线 OHLCV；`daily-indicators` 使用收盘价计算 SMA(5)、SMA(20) 和 Wilder RSI(14)。每个 API 返回行算一个观测，不补齐缺失交易日；达到窗口前指标为 `null`。这些指标仅描述历史数据，不构成策略信号。

```bash
python -m a500_t_trader daily-history --symbol 510300 \
  --start-date 2026-10-01 --end-date 2026-10-30 --database history.sqlite3 \
  --confirm-history-persistence-permitted
python -m a500_t_trader daily-indicators --symbol 510300 \
  --start-date 2026-10-01 --end-date 2026-10-30 --database history.sqlite3 \
  --confirm-history-persistence-permitted
```
