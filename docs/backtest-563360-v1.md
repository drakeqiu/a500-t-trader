# 563360 V1.0 回测准备记录

本记录同时保留研究输入审计和 Issue #83 探索性账户回放结果；不是认证报告。行情、起始账户及基本撮合输入已按下文核验并用于三组可复现对照。

**结论：Issue #83 三组探索性回放已完成；策略认证仍未完成。** 回放按统一的 100 份/信号研究数量规则模拟账户现金、持仓、T+1、订单、成交与费用。该规则不是 V1 `Q_capacity`/保护底仓仓位 sizing，不得据此宣称完整 V1 策略认证或实盘可执行。

比较组：KDJ15/RSI0 基线、KDJ8/RSI7 评分拆分、KDJ8/RSI7 评分拆分并加 RSI 动量过滤。共享同一行情、500,000 元空仓起始状态及成交/成本假设；结果及增量见“Issue #83 探索性账户回放”。

## Issue #82 输入审计状态

| 输入/门槛             | 仓库可核实的证据                                                                                                                                  | 状态与未满足项                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 目标行情区间          | 可用行情从 2024-10-15 上市日起至 2026-10-28；`data/market-history/563360_5m_20240601_20261028.json` 中 23,760 根实际数据覆盖该区间                | 上交所公告确认 563360 自 2024-10-15 起上市交易；AmazingData 对 2024-06-01 至 2024-10-14 返回 0 根。AmazingData `BaseData.get_calendar(market="SH")` 返回区间内 495 个上交所交易日，与行情覆盖的 495 天完全相等：缺 bar 日 0、非交易日 bar 日 0；每个交易日 48 根，每日均有成交。                                                                                                                                                                                                                       |
| 初始账户              | 用户在本次会话指定回测起始资金 500,000 元、空仓、无未完成订单/冻结资金、无负债                                                                    | 按首个决策 bar 前（2024-10-18 13:55）作为回测场景输入；空仓时账户资金按 500,000 元现金处理。这是用户指定的模拟起始状态，不代表已核验的券商历史账户。                                                                                                                                                                                                                                                                                                                                                   |
| 证券执行规则          | 上交所公告确认 563360 自 2024-10-15 起上市；ETF FAQ 说明股票 ETF 为 T+1、交易单位 100 份、最小变动 0.001 元；基金管理人上市公告载明涨跌幅限制 10% | 规则出处：[基金管理人上市交易提示性公告](https://pdf.dfcfw.com/pdf/H2_AN202410151640296343_1.pdf)、[上交所 T+0 FAQ](https://www.sse.com.cn/assortment/fund/etf/question/c/c_20240118_5734755.shtml)、[上交所交易单位 FAQ](https://www.sse.com.cn/assortment/fund/etf/question/c/c_20240118_5734754.shtml)、[563360 上市公告](https://www.sse.com.cn/disclosure/announcement/listing/c/c_20241014_10762667.shtml)。历史限价字段审计见本页核验状态：494/495 日字段有效，1 日无效；未观察到规则变化证据。 |
| 逐 bar 账户与订单状态 | 通过 `run_intraday_backtest` 回放各组逐 bar 现金、持仓、可卖量、冻结现金、委托、成交、撤单及基准账户                                              | Issue #83 runner 已可复现相同账户/撮合输入；每个技术候选固定下单 100 份，仅作探索性数量规则，不是 V1 `Q_capacity`、风险限额或保护底仓 sizing。                                                                                                                                                                                                                                                                                                                                                         |
| 成本与冲击/成交假设   | 用户佣金为万 0.5、最低每笔 0.1 元；用户同意下一根 bar 开盘价、0 滑点                                                                              | 采用下一根开盘价、不计滑点的理想化基准，佣金按既有配置计入；不是实际成交证据，真实冲击/滑点敏感性仍未覆盖。                                                                                                                                                                                                                                                                                                                                                                                            |
| 数值认证门槛          | V1 文档给出收益、Profit Factor、成本压力测试、稳定性及滚动样本外等定性/部分数值条件                                                               | 用户选择先做未认证的探索性对照；最大回撤预算、最低有效交易样本量、样本内外切分/滚动窗口长度及参数扰动幅度仍未量化，因此不得给出认证结论。                                                                                                                                                                                                                                                                                                                                                              |

日历、K线与历史交易状态核查已完成：AmazingData 上交所日历与行情日期集合完全匹配；495 个交易日均有 48 根 5 分钟 bar。1 分钟行情 119,790 个预期时间戳无缺失。AmazingData 历史 Level-1 快照 `trading_phase_code` 查询分 107 批、共 3,674,401 条，未发现 `P` 停牌状态；基金公告列表 155 条，其中 8 条含停牌关键词，均为基金持有停牌证券估值调整提示，不是 ETF 自身停牌公告。按用户要求信任供应商状态数据；这不是独立交易所逐分钟状态证明。行情异常与历史限价字段审计见下文。以上不再阻断未认证探索性对照。

## 已确认的用户输入

| 项目          |                               值 | 说明                                                                                                                 |
| ------------- | -------------------------------: | -------------------------------------------------------------------------------------------------------------------- |
| 回测区间      | 2024-10-15 上市日起至 2026-10-28 | 用户按上交所上市公告更正可回测行情起点；需先用上市后的 180 根已完成 bar 预热，首个可回放决策 bar 为 2024-10-18 14:00 |
| 原账户快照    |                       2024-05-31 | 仍保留原始用户输入供审计；该日记录的 563360 持仓早于上市日，不用于本回测                                             |
| 回测初始资金  |                       500,000 元 | 用户指定；按空仓、无冻结资金/未完成订单处理，作为 2024-10-18 13:55 的模拟场景                                        |
| 回测初始持仓  |                             0 份 | 用户指定空仓；70% 保护底仓为 0 份                                                                                    |
| 其他资产/负债 |                               无 | 用户确认无负债；无其他资产及订单状态                                                                                 |
| 保护底仓比例  |                              70% | 用户原规则；按本次初始持仓 0 份计算保护底仓为 0 份                                                                   |
| 佣金费率      |                万 0.5（0.5 bps） | 用户提供                                                                                                             |
| 最低佣金      |                      每笔 0.1 元 | 用户提供                                                                                                             |
| 回测规则      |  场内 ETF 日内做 T 量化策略 V1.0 | 来源 `docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md`                                  |

旧的 2024-05-31 账户输入仍保留作审计记录，但因其记载 563360 持仓早于上市日，不用于本次回测。用户现指定以 500,000 元、空仓、无未完成订单/冻结资金/负债作为 2024-10-18 13:55 的模拟起始状态；该状态不是券商历史账户证明。回测行情区间为上市日起至 2026-10-28。不得将模拟状态表述为真实账户复盘。

## 可用行情与本次拉取

仓库原有两份一分钟样例（2026-10-30 与 2026-11-02）晚于目标结束日，不能用于目标区间。

已使用本机 AmazingData 配置请求 563360 的 5 分钟历史，并保存为 `data/market-history/563360_5m_20240601_20261028.json`：

- 返回并保存 23,760 根已完成 5 分钟 K 线，首根为 2024-10-15 09:30，末根为 2026-10-28 14:55（北京时间）。
- [上交所上市公告](https://www.sse.com.cn/disclosure/announcement/listing/c/c_20241014_10762667.shtml) 确认 563360 自 2024-10-15 起上市交易；另对 2024-06-01 至 2024-10-14 的历史查询返回 0 根。因此该上市前区间不再列为行情缺口。
- 文件记录了原始请求范围、AmazingData 来源和各根 K 线。当前只核验首末时间戳、数量及 `is_completed`；尚未逐交易日对照交易日历验证缺日/缺 bar。
- 数据可用时间自上市日开始；按前 180 根已完成 bar 初始化，bar #180 为 2024-10-18 13:55，bar #181（首个可回放决策 bar）为 2024-10-18 14:00。
- 本地行情文件已写入；用户明确授权保存。AmazingData 登录成功，Provider 返回数据。

## 已确认账户与策略约束

V1 通用底仓下限为 30% NAV；用户本次明确的回测账户初始持仓为 0 份，因此起始 70% 保护份额为 0 份，初始普通卖出容量也为 0 份。后续买入份额是否受 70% 保护及其重算口径，须按策略规则实现；不得沿用旧快照的 400,700 份库存和 280,500 份保护底仓。V1 文档默认的 30% NAV 底仓下限与该初始份额保护规则是不同约束。

V1 参数来源为 `docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md`。关键规则包括 5 分钟线、EMA20/60、BOLL(20,2)、KDJ(9,3,3)、Wilder ATR14、每日重置 VWAP、180 根预热、评分及信号阈值、100 份整手、净资产和单笔风险限制、成交额/价差/ATR 流动性过滤、T+1 与配对买回约束。完整规则以该文件为准。

Issue #83's source-rule comparison remains in its own module; the V1 sequential Issue #85 replay and its public runner now live in `issue85_replay.py`, alongside their dedicated chronological-validation logic. The Issue #83 comparison remains a separate fixed-quantity research path, not the V1 replay. Issue #85's sequential replay adds dynamic candidate sizing and the V1 `Q_liquidity` cap, but remains a research simulation, not complete V1 certification.

## 认证门槛的已知与缺失

策略文档明确了：扣费后总收益为正、配对交易平均净利为正、参考 Profit Factor≥1.25、样本内外 Profit Factor 均>1、佣金与滑点提高 50% 后重测、相邻参数稳定、按时间顺序滚动训练和样本外测试，并模拟委托/部分成交/撤单/资金冻结/成本/库存/T+1。

文档未量化最大回撤上限、最低有效交易样本量、样本内外切分/滚动窗口长度，以及参数扰动的具体幅度。“足以评估稳定性”“风险预算内”“不发生断崖式恶化”不是可自动判断的数值条件。认证前必须先固定这些值，避免看过结果后再选择门槛。用户所说“不明白认证门槛”可简单理解为：这些是策略要达到的历史表现标准，但有几项仍没有具体数字。

## Issue #83 探索性账户回放

复现命令：

```bash
uv run python -m a500_t_trader intraday-compare-563360 \
  --bars data/market-history/563360_5m_20240601_20261028.json \
  --output issue83-comparison.json
```

输入 SHA-256：`0da5fdfe8f20dd1ba40ff5393ea31240f9df8e920bf6cbdbcbcd26ba3d45053c`；规则源文件 `docs/strategy/intraday-etf-t-v1/intraday_etf_t_quantitative_strategy_v1.0.md` SHA-256 `ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4`（runner 输出 `rule_source_sha256`）；策略 ID `intraday-etf-t-v1-rsi`，技术参数哈希 `1372d7e6bc868a77827cce95488a90dccc053613bc41da1888e4b665f5678009`。取 180 根预热 bar，回放 23,580 根决策 bar（2024-10-18 14:00 至 2026-10-28 14:55）。同一账户为 2024-10-18 13:55 现金 500,000 元、0 持仓、0 未完成订单、0 冻结资金、0 负债。下单固定每候选 100 份；下一可用 bar 开盘成交，滑点 0 bps、佣金 0.5 bps、最低每笔 0.1 元、成交参与率 2%、每笔委托寿命 1 bar、100 份整手及 T+1；零成交量 bar 不成交。

| 规则组                   | 权重 (EMA/BOLL/KDJ/RSI/VWAP) | RSI 动量过滤 | 信号数 | 成交数 (买/卖) | 期末现金 + 持仓         |     期末账户值 | 相对前组增量 |
| ------------------------ | ---------------------------- | ------------ | -----: | -------------: | ----------------------- | -------------: | -----------: |
| KDJ15/RSI0 基线          | 20/20/15/0/25                | 否           |    110 |     39 (20/19) | 499,914.000 元 + 100 份 | 500,026.300 元 |         基线 |
| KDJ8/RSI7 评分拆分       | 20/20/8/7/25                 | 否           |    109 |     48 (24/24) | 500,010.100 元 + 0 份   | 500,010.100 元 |   -16.200 元 |
| KDJ8/RSI7 + RSI 动量过滤 | 20/20/8/7/25                 | 是           |    108 |     48 (24/24) | 500,010.100 元 + 0 份   | 500,010.100 元 |     0.000 元 |

三组结果为这套固定一手数量及理想化撮合参数下的历史模拟，不是统计显著性或盈利优势证明。第三组少 1 个信号，但终值相对第二组无变化；信号减少不构成改善证据。首组期末仍有 100 份持仓，账户值按末根完成 bar 收盘估值。报告还记录订单撤单、逐笔成交、共同撮合假设及组间增量；当前结果不得用于实盘决策或策略认证。

仍未满足的认证/完整 V1 条件：数值化最大回撤限额、最低有效样本、样本内外划分与滚动窗口、参数压力范围；V1 动态账户仓位/容量与保护底仓执行；经独立认证的行情来源完整性、公司行为和实际成交/冲击证据。以下结果只解除 Issue #83 探索性比较前置阻塞。

## 当前回测核验状态

1. 行情文件共 23,760 根 bar，495 个交易日、每个交易日 48 根。交易日与 AmazingData 日历一致；OHLC 为正且高低价包含开收盘价，价格符合 0.001 元 tick；逐日收盘涨跌幅未超过 10.05% 容差。5 根成交量与成交额同时为 0（2025-05-16 11:05、2025-05-27 14:05、2025-05-30 13:20、2025-06-06 14:00、2025-06-18 14:10），容量为 0，不成交。
2. AmazingData 历史交易阶段查询覆盖回测期，未发现非交易状态记录；按用户要求信任供应商数据。该核验不是独立交易所逐分钟状态源。
3. 初始账户及执行假设与比较 runner 一致：500,000 元现金、空仓、无未完成委托/冻结资金/负债；下一可用 bar 开盘价、0 滑点、0.5 bps 佣金、最低每笔 0.1 元、2% 成交参与率、委托有效 1 bar。
4. 涨跌幅审计：基金管理人上市公告载明 10% 限制。AmazingData 每日限价字段 495 日中 494 日有效，推算比例为 9.94%–10.06%，偏差符合 0.001 元报价单位取整；2026-04-20 两个限价字段为 0/无效。该日 5 分钟行情高 1.334、低 1.319，未接近日限价。未发现规则变化证据，但无效字段日不计作字段逐日确认。
5. 下方固定 40,000 份信号文件是另一项候选信号分析，不是 Issue #83 的成交回放。Issue #83 对照使用同一策略信号源与账户撮合，但每个候选订单为 100 份；结果只对应该研究数量规则。
6. 最大回撤上限、最低有效交易数、训练/样本外划分及参数扰动幅度尚未量化；这阻断认证，不阻断已标明未认证的探索性比较。

## 佣金实现回归记录

- 佣金口径：比例佣金按成交额乘以 0.5 bps；单笔最低 0.1 元按每个模拟 fill 独立应用，买入和卖出均适用。部分成交每一笔分别计算最低佣金。
- 实现位置：基础价格触发回测 `run_backtest`、日内回放 `run_intraday_backtest`；CLI 提供 `--minimum-commission` 参数，默认 0（禁用最低佣金），费用参数随回测结果持久化。
- 测试命令：`uv run pytest -q tests/test_intraday_backtest.py tests/test_backtest_cli.py`。
- 结果：12 项通过；覆盖日内部分成交逐笔最低佣金、CLI 参数持久化，以及两套回测路径既有行为。
- 静态检查：相关 Python 源码及测试的 Ruff 检查通过；回测、日内回放和 CLI 源文件的 mypy 检查通过。
- 限定：这组回归测试覆盖佣金计算与回测工具行为；Issue #83 的实际行情账户回放及其限制见前文。本次结果仍不是 V1 动态 sizing/保护底仓完整回测，也不是认证。

## 当前实现的边界

- `python -m a500_t_trader backtest` 是简单价格触发回测，要求显式 `buy_below` / `sell_above` 规则；它不是 V1.0 策略执行器。
- V1 分析/模拟 API 要求调用方提供数量解析器；最低佣金通过执行参数显式设置。
- 回测认证接口只评估调用方提供的证据和配置；它不会生成完整 V1 策略回测，也不会推定认证阈值。
- 所有模拟成交均非实际成交；历史认证不是未来收益保证，也不是下单授权。

本次已完成 Issue #83 指定的探索性账户比较；不等于完整 V1 仓位策略执行回测。2024-10-15 上市日起至 2026-10-28 的行情用于本次研究，且不能代表策略认证或实盘证据。

## 固定份额技术候选信号回放

- 命令：`uv run python -m a500_t_trader intraday-signals --output data/563360_v1_signals_20241015_20261028.json`；默认数量 40,000 份。
- 实际输入：23,760 根已保存的 5 分钟 bar，首根 2024-10-15 09:30，末根 2026-10-28 14:55；行情自上市日起，无上市前交易数据。
- 输出：111 个技术候选，其中 BUY 25 个、SELL 86 个；每个信号标记 40,000 份。首个为 2024-10-31 10:40 SELL，最后一个为 2026-10-27 11:05 BUY。
- 起始账户：用户指定的 2024-10-18 13:55 模拟输入为 500,000 元现金、0 份持仓、无订单/冻结资金/负债；不是已核验的券商历史账户。
- 分析设置：181 根滚动输入（含当前 bar，至少 180 根预热）；网格价位按上交所 ETF 规则 0.001 元，网格成本按每边万 0.5 佣金且每笔最低 0.1 元计算；不计滑点。
- 限定：这只是 V1 技术候选信号，不模拟成交、持仓更新、T+1、现金/卖出容量、配对账本或网格层级冷却；重复候选可能出现，信号数量不代表可执行数量。AmazingData 历史状态快照核查未发现临停，但不得据此计算回测收益或宣称策略认证。

## Issue #85 认证标准预注册

本节标准先于任何 Issue #85 动态账户回放结果固定。仅当动态回放及输入核验完成后，按以下标准判定；缺失必要证据时结论只能为“未认证”，不以指标达标代替缺失项。

| 标准 | 预注册门槛 | 失败处理 |
| --- | --- | --- |
| 扣费后总收益 | > 0 | 未通过 |
| 配对交易平均净利 | > 0 | 未通过 |
| 总 Profit Factor | ≥ 1.25 | 未通过 |
| 最大回撤 | ≤ 初始 NAV 的 15% | 未通过 |
| 有效闭合配对样本 | ≥ 100 | 未通过 |
| 时间切分 | 前 70% 交易日样本内，后 30% 样本外；按交易日边界切分，禁止随机打散 | 不满足时未认证 |
| 样本内/外 PF | 各自 > 1.0 | 未通过 |
| Walk-forward | Five expanding training prefixes followed by contiguous later test blocks; at least 4 test windows net-positive with PF > 1 | Each window runs independently; training and test gates are undefined when no closed-pair sample can define PF, otherwise the registered net/PF criteria are calculated. An undefined gate is not evidence of a pass. |
| 成本压力 | Fees and single-side slippage each multiplied by 1.5; overall net > 0 and PF > 1 | Any failed criterion is not passed; with baseline slippage at 0 bps, scaling remains 0 bps |
| 参数压力 | Each of the five signal-score thresholds individually ±10% (rounded to nearest integer), unchanged other rules; every replay net > 0 and PF > 1 | Any failed perturbation is not passed |

起始账户沿用 2024-10-18 13:55 用户定义的 500,000 元现金空仓研究场景；最大回撤按逐 bar 收盘 NAV 峰值到谷值计算。有效样本定义为有实际模拟买入及其关联卖出买回完成的闭合策略配对；未配对仓位、单边成交和取消委托不计有效样本。上述标准是本次研究的预先门槛，不构成策略收益保证。

### Issue #85 执行与结果状态

1. The sequential candidate path now uses the source analyzer and RSI momentum filter, fill-updated lots/pairs, ATR/grid metadata, risk exits, take-profit/buyback eligibility, 20-session complete-turnover eligibility, and the dynamic account simulator. The simulator models order submission/fill capacity, cash reservation, T+1 sellability, daily order/turnover/sell limits, and partial fills. Dedicated behavior tests do not cover each rule and edge case, so this remains a candidate replay rather than proof of complete V1 fidelity.
2. The corrected replay completed 23,580 decision bars (2024-10-18 14:00 through 2026-10-28 14:55) after 180 warm-up bars, starting with ¥500,000 cash and 0 holdings. Data SHA-256: `2c37f69cb258699b62b5613b9513915ac4b104c116c987db6d9a9a48f58c5d6a25`; strategy source SHA-256: `ea086d76c039ee9483f0d58bb17eb658811567e98cfd99ee79af6da3d7781bf4`; replay source SHA-256: `1aa410ef3bf61115e9ba6bd39e178c4eea8b8f922ac9cbbc078b1543a8f8b0d2`; account simulator SHA-256: `40a5325e7146ab24a72ec8ee866c8292c1328196875c67eaee66f4934fcf9b8a`. Recorded assumptions: next-bar open, 0 bps slippage, 0.5 bps commission, ¥0.1 per-fill minimum commission, 2 bps modeled impact cost, 2% OHLCV participation, and one-bar order lifetime.
3. Result: 49 submitted signals and 49 fills; final cash/NAV ¥497,937.846645, 0 holdings, net P&L -¥2,062.153355 (-0.412430671%), maximum bar-close drawdown 0.561911%. Net-return gate fails; drawdown gate passes. There are zero closed pairs, so paired average P&L and PF are undefined; the ≥100 pair requirement fails. Five expanding walk-forward training/test runs were executed: each test block contains zero closed pairs, so every test PF is undefined and the walk-forward gate is unassessed/failed. No stability inference is available.
4. Walk-forward uses a 50% initial training prefix followed by five contiguous 10% later test blocks. The training prefix expands for each fold; each segment has a fresh ¥500,000 account and the preceding 180 bars for indicator warm-up. Test windows: 2025-10-23–2025-12-30, 2025-12-31–2026-03-19, 2026-03-20–2026-06-02, 2026-06-03–2026-08-11, and 2026-08-12–2026-10-28. All have zero closed pairs; gate status is `undefined`, not a pass. Exact fold metrics are in the tracked replay artifact `data/backtests/563360_issue85_replay.json.gz`.
| Full-data stress | Threshold | Net P&L (¥) | Closed pairs | Gate result |
| --- | ---: | ---: | ---: | --- |
| 1.5× modeled execution-cost stress | — | -2,108.7603375 | 0 | Fail; PF undefined |
| Buy −10% | 65 | -5,429.789560 | 0 | Fail; PF undefined |
| Buy +10% | 79 | -804.079690 | 0 | Fail; PF undefined |
| Sell −10% | 61 | -2,062.153355 | 0 | Fail; PF undefined |
| Sell +10% | 75 | -2,062.153355 | 0 | Fail; PF undefined |
| Take-profit −10% | 54 | -1,914.573925 | 0 | Fail; PF undefined |
| Take-profit +10% | 66 | -1,221.259665 | 0 | Fail; PF undefined |
| Buyback −10% | 61 | -2,062.153355 | 0 | Fail; PF undefined |
| Buyback +10% | 75 | -2,062.153355 | 0 | Fail; PF undefined |
| Direction delta −10% | 11 | -2,062.153355 | 0 | Fail; PF undefined |
| Direction delta +10% | 13 | -2,062.153355 | 0 | Fail; PF undefined |

The 1.5× stress scales commission, minimum commission, modeled impact and the slippage input (which remains zero). The 0.02% per-side impact is a cost-grid assumption only; it is not applied again to simulated fill prices and is not empirical impact evidence. The cost stress is net-negative with no eligible pair sample; every threshold perturbation is also net-negative with no pairs. All corresponding gates fail.
5. **Not certified.** Independent market/limit data, complete corporate-action treatment and real fill/impact evidence remain unavailable; the 2026-04-20 daily limit fields are invalid. These evidence gaps cannot be repaired by the simulator and must not be inferred. The replay is still a candidate-driven sequential simulation, not proof of every source-exact V1 rule; Issue #85 remains open.
The historical bars contain OHLCV only; they do not contain quote-level bid/ask data, so the required spread threshold (`≤max(0.15%, 2 tick/P)`) is **unknown**, not satisfied. The dynamic account simulator now fails closed for ordinary BUY and BUYBACK signals unless spread eligibility is explicitly evidenced. No such evidence exists for this replay. Accordingly the historical fill/NAV/stress figures above predate this guard and are not valid V1-eligible replay results; they remain descriptive candidate-path outputs only. The source dataset cannot establish spread eligibility, and the full replay artifact has not been regenerated under this fail-closed rule.

V1 account sizing is constrained per order to the minimum of risk quantity, 8% NAV notional, position headroom up to 65% NAV, trading-sleeve headroom up to 25% NAV, 2% of last completed bar volume, available cash above the 35% NAV reserve, and any pair-specific buyback remainder, then floored to 100-share lots. Pending buys and reserved cash consume capacity. Fees are reserved in affordability checks; fills recheck position/trading capacity and affordability. The ordinary sell floor remains 30% NAV and the initial protected holding remains 70% of the starting inventory, subject to T+1 sellability.
### Issue #85 evidence and implementation audit

The official [SSE listing notice](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-10-15/563360_20241015_S2M6.pdf) establishes 563360's 2024-10-15 listing and 10% daily price limit. The [SSE ETF FAQ](https://www.sse.com.cn/assortment/fund/etf/question/) supports the general stock-ETF T+1, 100-unit, and ¥0.001-tick rules. These sources do not establish exact daily limit prices for the whole replay interval, including 2026-04-20. The [SSE historical-data services page](https://www.sse.com.cn/services/tradingtech/historicaldata/) and [SSE data-product page](https://english.sse.com.cn/markets/dataservice/products/) describe acquisition paths for historical Level-1/Level-2 data; no instrument/date-specific independent files were acquired.

No complete official 563360 corporate-action timeline and treatment audit, independent daily market/limit-field series, historical spread/depth series, or verified broker fill and contemporaneous impact records are available in this workspace. No search result is proof of no corporate actions. AmazingData's 2026-04-20 zero/invalid limit fields remain unresolved and its files are not independent verification. Replay inputs record 0 bps slippage as an explicit baseline assumption (source: exploratory baseline assumption recorded in this report; recorded 2026-11-03; context: idealized next-available-bar-open baseline), marked `observed: false`; it is not actual-fill or market-impact evidence.

The public account simulator rechecks dynamic buy capacity at the fill price as well as at order submission. The Issue #85 sequential policy seeds the candidate-cost/grid iteration at zero cost (no synthetic share count), then derives cost from the current ATR/grid risk-sized candidate quantity: ordinary BUY/SELL, paired BUYBACK remaining quantity and BEAR 25%-of-original cap, or TAKE_PROFIT lot/sellability limits. Replay inputs and assumptions record 0.5 bps per-side commission, ¥0.1 minimum commission per fill, 0 bps baseline slippage, 2 bps per-side modeled impact and the cost multiplier; every assumption states units, source, context and whether it is observed. Modeled impact is used in candidate-cost estimates but is not applied again to simulated fill prices. The 1.5× stress preserves its multiplier and scaled execution inputs. The policy permits eight candidate/quantity passes; if they do not stabilize, it analyzes once more using the highest rate observed, preserving a conservative wider grid. A candidate with no permitted board lot contributes no new cost; any previously observed higher cost is retained.

Sequential sizing applies ordinary ATR/grid risk sizing where prescribed, free-cash, turnover, addition/fill and sell-notional caps, the 30% holding floor for ordinary sells, TP lot and sellability constraints, and paired BUYBACK remaining quantity and the BEAR cap. The V1 Q_liquidity quantity cap applies to BUY/SELL, BUYBACK, TAKE_PROFIT and risk-exit candidates: floor to 100-share lots of 2% of the last completed decision bar's volume. This is distinct from the simulator's next-bar fill participation cap. The public simulator rechecks capacity at order submission/fill, so actual fills may be smaller or rejected. The audit and public-seam behavior test report candidate quantities against decision-bar volume; no independent spread/depth or execution evidence is implied.
For the daily loss guard, each new session's starting NAV is the prior completed decision bar's NAV, not the first decision bar's post-gap NAV. The serialized replay includes session-reset observations (`previous_close_nav`, `first_decision_nav`, `daily_start_nav`, and first-decision return), so the opening-gap loss remains inside the daily drawdown window and is directly auditable.

The replay accepts an optional `initial_cash` amount for public, sequential account-size behavior checks; its default remains ¥500,000 and the default replay is unchanged. The test uses a synthetic ¥10,000 starting account: at the strategy's 8% NAV per-order cap and sampled ETF prices, this permits eligible 100-share board-lot BUY candidates while keeping their trade notionals below the ¥2,000 per-side minimum-commission crossover. This is a replay stress input, not observed market or broker-account evidence, and is not a replacement for the documented ¥500,000 research scenario. Cost rates are not generally quantity-sensitive: above the per-side minimum-commission crossover (¥0.1 / 0.5 bps = ¥2,000 notional), proportional commission plus impact yields a constant 0.05% round-trip rate. Below that crossover, the ¥0.1 per-side floor makes the percentage cost higher on smaller eligible BUY trades. The behavior test runs the fixed fixture through the public sequential replay at both cash levels and independently verifies eligible BUY audit quantities in each regime.

The baseline replay output includes all 23,580 per-bar account states; the full replay artifact also preserves the cost and threshold stress results and five walk-forward folds. It is gzip-compressed with a zeroed timestamp for reproducibility. Certification remains **NOT CERTIFIED** until source fidelity and independent market, corporate-action and execution evidence meet the registered gates. Keep Issue #85 open.
6. Replay command used to regenerate the tracked artifact:

```bash
mkdir -p data/backtests
uv run python -c 'import gzip, json; from pathlib import Path; from a500_t_trader.cli.intraday import _load_completed_bars; from a500_t_trader.backtest.issue85_replay import run_issue85_sequential_replay; bars=_load_completed_bars("data/market-history/563360_5m_20240601_20261028.json"); result=run_issue85_sequential_replay(bars); raw=json.dumps(result, indent=2, sort_keys=True, default=str).encode(); path=Path("data/backtests/563360_issue85_replay.json.gz"); path.parent.mkdir(parents=True, exist_ok=True); stream=path.open("wb"); compressed=gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=9); compressed.write(raw); compressed.close(); stream.close()'
```

The artifact stores `account_states` for every baseline decision bar, plus run inputs, assumptions, fills-derived metrics, stresses and walk-forward folds. Reproduction produced 101,766 compressed bytes and 9,699,860 uncompressed bytes; gzip SHA-256: `96d5ddcfe6ff1d15e4df52f832c8717566074cd673aaee53571c0d5f41a29a38`. Integrated test/build status is intentionally unchanged by this review fix; the parent integration owner will rerun the requested checks.
