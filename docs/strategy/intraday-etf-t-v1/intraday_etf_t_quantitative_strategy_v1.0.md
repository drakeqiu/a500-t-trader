# 场内 ETF 日内做 T 量化策略 V1.0

策略类型：5分钟级别 · 多因子融合 · ATR动态网格 · VWAP均值回归 · 持仓状态机 · 风险优先

本策略使用 EMA20/EMA60 判断趋势、BOLL 判断价格偏离、KDJ 判断短线拐点、RSI 判断动量恢复或衰减、VWAP 判断均值回归空间、ATR 动态网格判断交易位置，并结合持仓、可卖数量、交易成本和风险约束生成最终信号。
输出五种意图：BUY 买入、SELL 卖出、WAIT 等待、TAKE_PROFIT 止盈、BUYBACK 买回。每个信号输出方向、0–100 强度分数、触发原因、建议数量、参考价格和风险状态。
说明：本文参数为可执行的初始规则设计，尚未证明是回测最优参数；评分是规则匹配度，不是盈利概率。
2026-11-03修订：直接在V1.0中加入RSI；KDJ/RSI共享15分动量预算，其余权重、交易阈值、网格、仓位和止损参数保持原值。本文为修订后的目标规则，当前计算代码尚未接入RSI；不能将现有程序输出视为符合本文，也不能沿用修订前的回测认证。规则身份以文件SHA-256为准，不能只凭V1.0名称判断一致。

## 一、适用范围与交易制度

境内股票 ETF：通常 T+1，使用昨日及更早可卖持仓先卖后买；当日新买份额不得当日卖出。
跨境 ETF：符合规则的产品通常可以 T+0。
黄金、债券 ETF：符合规则的产品通常可以 T+0。
货币 ETF 及部分商品期货 ETF：具体回转规则、流动性和成本需逐只确认。
ETF 二级市场常以100份为一个交易单位，报价最小变动单位通常为0.001元，具体应以标的最新交易规则和券商账户可卖库存为准。

## 二、策略架构

已完成的5分钟线 → EMA/BOLL/KDJ/RSI/VWAP/ATR指标 → 双向评分 S_B/S_S →
RSI动量、持仓和风险过滤 → BUY/SELL/WAIT/TAKE_PROFIT/BUYBACK →
下一可交易时刻执行 → 成交回报更新状态机。
风险否决高于技术评分。技术评分高不意味着一定允许发单。

## 三、指标定义与参数

EMA快线：20周期，5分钟收盘价。
EMA慢线：60周期，5分钟收盘价。
BOLL：(20, 2)，中轨 SMA20，上下轨为中轨 ± 2×标准差。
KDJ：(9, 3, 3)，RSV9，K、D递推平滑。
RSI：14周期，5分钟收盘价，Wilder平滑；超卖/超买参考线30/70，持续恢复/衰减评分边界45/55。
ATR：14，Wilder平滑。
VWAP：当日累计成交额 / 当日累计成交份额，每天9:30重置。
动态网格：基础 0.9×ATR，同时受成本和最小幅度约束。
指标预热：至少180根有效5分钟K线。

EMA、BOLL、ATR、KDJ、RSI跨交易日连续计算，不能在午休或每天开盘重置。只使用已完成K线确认，不能用正在形成的K线；不为休市时段补造K线。

KDJ：

```text
RSV_t = 100 × (C_t - LLV9) / (HHV9 - LLV9)
K_t = (2/3) × K_(t-1) + (1/3) × RSV_t
D_t = (2/3) × D_(t-1) + (1/3) × K_t
J_t = 3 × K_t - 2 × D_t
初始 K、D 为50；若分母为0，RSV设为50。BOLL使用总体标准差。
```

RSI：
设N=14，u_t=max(C_t-C_(t-1),0)，l_t=max(C_(t-1)-C_t,0)。
首个平均涨幅U、平均跌幅L分别取最初14个有效收盘价差的u、l算术均值，需要至少15根收盘价；其后递推：

```text
U_t=((N-1)×U_(t-1)+u_t)/N
L_t=((N-1)×L_(t-1)+l_t)/N
R_t=100×U_t/(U_t+L_t)。
U=L=0时R=50；仅L=0时R=100；仅U=0时R=0。R_t即RSI14；不可用普通EMA的2/(N+1)系数或简单滚动平均替代。
```

预热仍至少180根有效5分钟线，并要求当前及前一根RSI均有效。缺失值不补成50，不根据未完成K线判断穿越。
30/70是参考区间，不是独立买卖指令；强趋势中RSI可能长期超买或超卖。RSI背离本版不参与评分，避免引入未定义的摆动点和未来数据。

### 3.1 EMA 趋势过滤

设 d_t=(EMA20-EMA60)/ATR，m_t=(EMA20_t-EMA20_(t-3))/ATR。
BULL：d≥0.20 且 m≥0.05；优先回调买入。
BEAR：d≤-0.20 且 m≤-0.05；优先反弹卖出。
RANGE：其他情况；优先均值回归。
STRONG_BULL：d≥0.90 且 m≥0.25。
STRONG_BEAR：d≤-0.90 且 m≤-0.25。
强趋势是普通趋势子状态，不重复加分。
BULL正常允许买入、买回、止盈；RANGE允许双向但需要确认；BEAR禁止普通新BUY，弱BEAR仅在严格条件下允许BUYBACK；STRONG_BEAR禁止BUY和BUYBACK。

## 四、多因子评分与融合

权重（合计100分）：EMA趋势20、BOLL20、KDJ8、RSI7、VWAP25、ATR网格20。
KDJ与RSI均属于动量证据，因此将原KDJ的15分拆为8+7；不额外增加总分，也不因增加指标而降低交易门槛。该分配是待验证的初始假设，不代表统计最优。
对买入和卖出分别计算独立评分。设 clip(x)=min(1,max(0,x))。

### 4.1 EMA 评分（最高 20 分）

BULL：买20，卖0。
RANGE：买10，卖10。
BEAR：买0，卖20。
强趋势不额外加分，由交易过滤处理。

### 4.2 BOLL 评分（最高 20 分）

令 z=(C-MB)/(2σ)，MB=SMA20，σ为20周期总体标准差。
B_B=20×clip((-z-0.35)/0.65)
B_S=20×clip(( z-0.35)/0.65)
z≤-1：买20；z=-0.75：买12.3；z=-0.50：买4.6。
z在-0.35至+0.35：两边均0。
z=+0.50：卖4.6；z=+0.75：卖12.3；z≥+1：卖20。
只提供偏离证据，触轨不直接下单。

### 4.3 KDJ 评分（最高 8 分）

K上穿D且交叉前后最低K≤35：买8。
K>D、K继续上升且K≤45，但未新金叉：买4。
K下穿D且交叉前后最高K≥65：卖8。
K<D、K继续下降且K≥55，但未新死叉：卖4。
其他状态：0。
金叉：K_(t-1)≤D_(t-1) 且 K_t>D_t；死叉对称。
J值异常高低只记录，不加分，以免重复计算。

### 4.4 RSI 评分（最高 7 分）

以下每个方向按顺序匹配，单根K线只取该方向一档，不累加：
R_(t-1)≤30且R_t>30：买7，记RSI_OVERSOLD_RECOVERY。
否则，30<R_t≤45且R_t>R_(t-1)：买4，记RSI_RECOVERY_CONTINUES。
R_(t-1)≥70且R_t<70：卖7，记RSI_OVERBOUGHT_COOLDOWN。
否则，55≤R_t<70且R_t<R_(t-1)：卖4，记RSI_COOLDOWN_CONTINUES。
其余为0；仅处于R≤30或R≥70不加分。R=30不算向上脱离，R=70不算向下脱离；RSI不变不加分。
45至55附近不因接近中轴而加分，恰好45/55按上述闭区间处理。
RSI评分只占7分；第六部分的动量过滤单独决定普通意图是否允许。过滤通过不代表必有RSI加分。

### 4.5 VWAP 均值回归评分（最高 25 分）

VWAP_t=ΣAmount_i/ΣVolume_i，当日累计；金额与份额单位必须统一。
令 v=(C-VWAP)/ATR。
V_B=25×clip((-v-0.30)/0.90)
V_S=25×clip(( v-0.30)/0.90)
v≤-1.20：买25；-0.90：买16.7；-0.60：买8.3。
-0.30至+0.30：均0。
+0.60：卖8.3；+0.90：卖16.7；≥+1.20：卖25。
强下跌趋势中不能仅凭VWAP偏离买入。

### 4.6 ATR 网格评分（最高 20 分）

锚点A，间距g；L_k=A-k×g，U_k=A+k×g，k=1,2,3。
最近2根已完成K线触及未使用下网格、当前收盘反弹到该网格之上：买20。
触及下网格、收盘位于网格下方0.1g以内但未反弹：买10。
最近2根已完成K线触及未使用上网格、当前收盘回落到该网格之下：卖20。
触及上网格、收盘位于网格上方0.1g以内但未回落：卖10。
未触发或已经使用过：0。若多层同时触及只选最深层，单向网格分上限20。
10分提示不等于下单许可。

### 4.7 综合分

S_B=T_B+B_B+K_B+R_B+V_B+G_B
S_S=T_S+B_S+K_S+R_S+V_S+G_S
Δ=S_B-S_S。仅当|Δ|≥12时方向明确。内部保留浮点值，显示时四舍五入。
0–39：很弱，不交易；40–59：偏弱，观察；60–69：中等，可进入额外约束严格的止盈或买回判定；70–84：较强；85–100：很强，但不突破风控。
BOLL、VWAP和网格高度相关，KDJ与RSI也可能重复反映动量，综合分不是统计意义上的胜率。

## 五、ATR 动态网格

价格P、ATR14、tick最小报价单位、预计一次完整往返成本率b。
趋势系数f：强趋势1.20，其他1.00。
r_g=max(0.9×f×ATR/P, 0.0025, 3b, 4×tick/P)。
若r_g>0.009则暂停新增交易，不把间距硬压回0.9%。
否则g=tick×ceil(P×r_g/tick)。
网格上下各3层，最小间距0.25%，最大允许网格比例0.90%，成本至少3倍，最小4个tick。

每天9:45固定锚点A=VWAP_09:45。
L1=A-g，L2=A-2g，L3=A-3g；U1=A+g，U2=A+2g，U3=A+3g。
当日不移动锚点，避免网格追价。
在10:00、10:30、11:00、13:30、14:00重新评价间距，变化达到20%才切换网格版本；需先处理未成交委托。已经成交的配对交易保留成交时的间距。

示例：A=1.213元，g=0.007元：
U3=1.234；U2=1.227；U1=1.220；A=1.213；L1=1.206；L2=1.199；L3=1.192元。
网格只是候选位置，需要已完成K线反转确认。
同层网格在一次周期中只能成交一次。价格回到锚点另一侧并持续2根已完成5分钟线，或次日，才能重置。同方向成交间隔至少2根已完成5分钟线。

## 六、交易意图及阈值

### 6.1 买入 K 线确认

C_t>C_(t-1)，且至少满足以下之一：
（1）当前有效KDJ金叉；
（2）此前触及BOLL下轨，当前收回轨内；
（3）最近2根K线触及下方网格，当前收盘回到该网格之上。
卖出确认对称：C_t<C_(t-1)，且KDJ有效死叉、上轨回落或上网格回落至少一个成立。
未确认时，即使评分90，也不产生普通交易指令。
RSI不能替代以上任一价格/KDJ确认项，不能凭RSI穿越单独产生交易。

RSI动量过滤（在上述确认之外额外检查）：
普通BUY和BUYBACK：R_t>30且R_t>R_(t-1)，记buy_rsi_allowed；避免RSI仍处于超卖或继续转弱时买入。
普通SELL：R_t<70且R_t<R_(t-1)，记sell_rsi_allowed；避免仅因RSI超买就在动量仍强时卖出。
R_t=R_(t-1)时，以上两项均不通过；缺失当前或前一根RSI时，普通BUY/SELL/BUYBACK均不允许。
TAKE_PROFIT保留盈利目标、卖出确认及评分要求，不增加sell_rsi_allowed硬门槛；止损、账户风险强制SELL和既有强制收尾不受RSI否决。RSI缺失时，这些退出路径的RSI分项记0并报告缺失，其余数据、评分和合法库存仍须满足对应规则。
此过滤可能延后入场或卖出、减少交易次数；是否改善净收益需用历史数据验证。

### 6.2 阈值

BUY：S_B≥72，Δ≥12，买入确认、buy_rsi_allowed、允许增仓，趋势不是BEAR。
SELL：S_S≥68，Δ≤-12，卖出确认、sell_rsi_allowed，存在合法可卖份额。
TAKE_PROFIT：S_S≥60，Δ≤-12，卖出确认，满足净收益目标。
BUYBACK：S_B≥68，Δ≥12，买入确认、buy_rsi_allowed，存在待买回配对卖单，并达到回落目标。
WAIT：其他情况，包括风险、数据、时间、库存、成本约束失败。
弱BEAR的BUYBACK须S_B≥78、有KDJ有效金叉、buy_rsi_allowed、连续2根已完成K线收盘上涨，数量≤原配对卖单数量的25%。STRONG_BEAR禁止买回，RSI超卖反弹也不能豁免。
普通BUY不能绕过BUYBACK配对数量约束。

### 6.3 TAKE_PROFIT

设配对持仓买入价P_e，买入时冻结网格g_e：
P_TP=P_e+max(g_e,3b×P_e)。
C_t≥P_TP、S_S≥60、Δ≤-12、已完成卖出确认、可合法卖出时，产生TAKE_PROFIT。

### 6.4 BUYBACK

对先卖出的成熟份额，记录成交卖价P_s、数量Q_s、当时网格g_s。
P_BB=P_s-max(g_s,3b×P_s)。
C_t≤P_BB、S_B达到对应阈值、Δ≥12、买入确认、buy_rsi_allowed、风控通过时，可买回。
买回数量≤对应尚未回补的配对卖单数量，且≤仓位上限。
例：卖价1.220元，冻结g_s=0.007元，且成本保护不超过g_s，则买回候选价≤1.213元。实际下单还须满足评分与确认。
当日没回落到目标，不强制追高回补。

## 七、交易成本与净收益过滤

可替换的示例假设：单边佣金费率0.015%，最低佣金5元，单边冲击成本0.02%，ETF二级市场交易通常不征印花税；以券商实际收费为准。
对委托份额q、价格P：
b(q,P)=2×max(5,0.00015×P×q)/(P×q)+0.0004。
举例：单笔成交12000元，两边佣金各5元，估计往返成本为10/12000+0.0004≈0.1233%。
必须要求预期配对价格空间至少为3倍预计往返成本率，否则不能新开交易。

## 八、持仓及风险管理

默认单ETF预算：底仓目标40%，交易仓上限25%，保留现金35%；不是必须一直持有65%。
单ETF最大仓位≤净资产65%；普通交易底仓下限30%；单笔委托≤净资产8%；单笔计划风险预算≤净资产0.15%；正常网格加仓每天最多3次；当日成交订单≤8笔；当日累计成交额≤净资产150%；同方向冷却期10分钟；普通卖出底仓每日额度≤净资产10%；买回份额≤配对已卖份额；多ETF组合总仓位≤净资产75%。
强制风控可突破底仓下限，但绝不突破可卖库存限制。

### 8.1 单笔数量

初始止损距离d_stop=max(1.5×ATR,1.2×g,0.004×P)。
风险金额R=0.0015×NAV。
Q_risk=R/d_stop。
Q=100×floor(min(Q_risk,0.08×NAV/P,Q_capacity,Q_liquidity)/100)。
不足100份WAIT；需另计手续费、冻结资金及未成交委托。

### 8.2 止损和移动保护

初始止损价Stop_0=P_e-d_stop。
T+0允许卖出的仓位在价格有利运行超过买入时一个网格后：
Stop_t=max(Stop_(t-1),Hclose_t-max(1.2×ATR_t,g_e),P_e×(1+b))。
Hclose为开仓后最高已完成5分钟收盘价。
已完成5分钟K线收盘≤止损价时触发风险卖出，不等待普通评分。
T+1当日新买份额标记unsellable_today，不得发出当日卖单；如无法卖出，则WAIT并报告风险退出受阻。

### 8.3 账户风控

日内已实现+浮动亏损≤-0.8% NAV：停止新买入和买回。
亏损≤-1.2% NAV：强制缩减合法可卖交易仓，停止新增。
单根5分钟异常波动>前一根ATR×2.5：暂停新开仓至少2根已完成K线。
数据断档、不完整：WAIT。
委托状态未知：禁止重复下单。
实际仓位超65%：禁止新增并优先调整风险。
任何强制卖出仍受合法可卖库存、可交易时段和实际成交约束。

## 九、交易状态机和优先级

每根已完成5分钟K线最多一个主意图。
优先级：
1 风险强制SELL（止损、最大亏损、异常保护）
2 TAKE_PROFIT（盈利目标+卖出确认）
3 BUYBACK（存在配对卖单且回落至目标）
4 普通SELL（S_S≥68、合法可卖）
5 普通BUY（S_B≥72、允许增仓）
6 WAIT（所有其他情形）

伪代码：

```python
def decide(ctx):
    if not ctx.closed_5m_bar:
        return wait("BAR_NOT_CLOSED")
    if not ctx.data_valid or not ctx.market_window_valid:
        return wait("DATA_OR_TIME_INVALID")
    if ctx.risk_exit_required:
        if ctx.sellable_qty > 0:
            return signal("SELL", 100, "FORCED_RISK_EXIT")
        return wait("RISK_EXIT_BLOCKED_T1")
    if ctx.risk_halt:
        return wait("RISK_HALT")
    buy_score, sell_score = calculate_scores(ctx)
    delta = buy_score - sell_score
    if (
        ctx.take_profit_eligible
        and ctx.sell_confirmed
        and sell_score >= 60
        and delta <= -12
    ):
        return signal("TAKE_PROFIT", sell_score, "PROFIT_TARGET")
    if (
        ctx.buyback_eligible
        and ctx.buy_confirmed
        and ctx.buy_rsi_allowed
        and buy_score >= ctx.buyback_threshold
        and delta >= 12
        and ctx.new_entry_allowed
    ):
        return signal("BUYBACK", buy_score, "PAIRED_BUYBACK")
    if (
        ctx.sell_confirmed
        and ctx.sell_rsi_allowed
        and ctx.sellable_qty > 0
        and sell_score >= 68
        and delta <= -12
    ):
        return signal("SELL", sell_score, "TECHNICAL_SELL")
    if (
        ctx.buy_confirmed
        and ctx.buy_rsi_allowed
        and ctx.normal_buy_allowed
        and ctx.new_entry_allowed
        and buy_score >= 72
        and delta >= 12
    ):
        return signal("BUY", buy_score, "TECHNICAL_BUY")
    return wait("NO_ELIGIBLE_SIGNAL")
```

eligible必须由利润、配对、仓位、时间、库存和流动性模块计算，不得拿分数替代。
buyback_eligible包含弱BEAR的额外约束及STRONG_BEAR禁买回；两个rsi_allowed由6.1计算，RSI缺失时均为false。data_valid检查行情完整性及退出所需数据，不将单独的RSI缺失等同于必须阻止保护性退出。
因RSI被阻断时，在blocked_reasons记录RSI_BUY_MOMENTUM_NOT_CONFIRMED、RSI_SELL_MOMENTUM_NOT_CONFIRMED或RSI_NOT_READY；风控退出的技术分仍独立记录。
已触发风险熔断时仍可执行保护性卖出；普通新增交易被禁止。

## 十、时间和流动性过滤

北京时间：
09:30–09:45：采集数据、初始化VWAP和网格。
09:45–09:50：验证流动性及网格。
09:50–11:20：正常做T。
11:20–11:30：不新增普通仓位，只允许符合条件的收尾和保护性卖出。
13:00–13:10：午后重新观察。
13:10–14:30：正常做T。
14:30–14:45：停止普通BUY，仍可合法BUYBACK和卖出。
14:45–14:50：仅保护性退出及T+0交易仓收尾。
14:50以后：不再生成新的普通交易委托。
这些是策略内部窗口，不代表交易所停止交易；收盘集合竞价机制应单独处理。

流动性门槛：20日平均日成交额≥3000万元；最近已完成5分钟成交额≥100万元；盘口价差≤max(0.15%,2tick/P)；单笔预计成交份额≤最近5分钟成交份额的2%；ATR/P处于0.08%–1.50%；有效历史数据≥180根。
任一不满足不得新开仓；风险退出按可成交条件另行处理。

## 十一、标准 JSON 输出示例

```json
{
  "symbol": "ETF_CODE",
  "bar_time": "YYYY-MM-DD HH:mm:ss",
  "timeframe": "5m",
  "bar_closed": true,
  "intent": "BUY",
  "direction": "LONG_INCREASE",
  "signal_strength": 90,
  "technical_buy_score": 90,
  "technical_sell_score": 0,
  "score_delta": 90,
  "trend": "BULL",
  "score_breakdown": {
    "ema": 20,
    "boll": 17,
    "kdj": 8,
    "rsi": 7,
    "vwap": 18,
    "atr_grid": 20
  },
  "market": {
    "close": 1.206,
    "vwap": 1.213,
    "atr14": 0.0074,
    "rsi14": 32,
    "previous_rsi14": 29,
    "grid_anchor": 1.213,
    "grid_step": 0.007,
    "grid_level": "L1"
  },
  "position": {
    "holding_qty": 10000,
    "sellable_qty": 10000,
    "pending_buyback_qty": 0
  },
  "execution": {
    "allowed": true,
    "suggested_qty": 1000,
    "reference_price": 1.206,
    "order_type": "LIMIT",
    "estimated_roundtrip_cost_rate": 0.00123,
    "take_profit_price": 1.213,
    "stop_reference_price": 1.195
  },
  "risk": {
    "status": "NORMAL",
    "forced": false,
    "t1_blocked": false
  },
  "trigger_reasons": [
    "EMA20_ABOVE_EMA60",
    "BOLL_LOWER_ZONE",
    "KDJ_GOLDEN_CROSS",
    "RSI_OVERSOLD_RECOVERY",
    "BELOW_VWAP",
    "GRID_L1_REBOUND_CONFIRMED"
  ],
  "blocked_reasons": []
}
```

注：示例数据仅用于展示字段，并非真实行情；分项与总分为展示舍入值，内部仍按未舍入值判定阈值。signal_strength=100用于强制风控SELL时，表示风控优先级，不代表技术胜率100%；仍应单独保留technical_sell_score。T+1无法强制卖出时应输出WAIT、allowed=false、risk.status="EXIT_BLOCKED"及受阻原因。

## 十二、评分规则试算

可以分别输入：EMA趋势状态（BULL/RANGE/BEAR）、BOLL标准化位置z、VWAP偏离ATR倍数v、KDJ状态（有效金叉/持续上行/有效死叉/持续下行/无信号）、前一根与当前RSI14、网格状态（下格反弹/下格附近/上格回落/上格附近/未触发）。
按第四部分逐项评分、计算S_B、S_S、Δ，检测评分门槛。静态文本无法保留原界面的交互滑块，实际评分由公式完整定义。
试算不代表最终交易许可，仍须K线确认、可卖库存、利润阈值、交易时间、T+1与风险规则。

RSI边界试算（前值→当前值；仅展示RSI分项及动量过滤）：
29→32：买7、卖0，允许买入动量；仍需价格/KDJ确认与总分。
32→34：买4、卖0，允许买入动量。
25→28：买0、卖0，不允许买入动量；超卖区内反弹不能直接抄底。
72→68：买0、卖7，允许普通卖出动量。
78→75：买0、卖0，不允许普通卖出动量；符合条件的止盈、风控退出仍可判定。
48→49：买0、卖0，允许买入动量但不加RSI分。
30→30或70→70：买0、卖0，两个方向的动量过滤均不通过。
总分即使已超过72，若RSI为25→28，也不产生BUY；强制SELL不因该RSI状态被阻断。

## 十三、回测验证与上线要求

1. 扣除所有交易成本后实现正收益。
2. 每笔配对交易平均净利润>0。
3. 盈利因子Profit Factor的参考要求≥1.25。
4. 样本内外盈利因子均>1。
5. 最大回撤位于事先设定的风险预算内。
6. 交易次数达到足以评估稳定性的样本规模。
7. 佣金和滑点提高50%后重新检验成本敏感性。
8. 相邻参数变化不引起收益断崖式恶化。
9. 使用按时间顺序的滚动训练与样本外测试，冻结规则再测试。
10. 分别模拟信号、发单、部分成交、撤单、资金冻结、成本、库存与T+1可卖数量。
11. 固定同一数据、成本、初始库存与撮合假设，对比修订前KDJ15/RSI0基线、仅拆分KDJ8/RSI7评分、以及评分加RSI动量过滤三组；单独报告新增评分与过滤的增量效果，不把交易减少等同于策略改善。
12. 分别报告震荡、上涨、下跌行情的净收益、最大回撤、Profit Factor、配对次数与平均净利润；特别检查卖出延后、买回延后和T+1风险暴露。
13. RSI邻近参数检验：周期12/14/16、参考线25/75、30/70、35/65，持续恢复/衰减边界40/60、45/55、50/50，动量权重KDJ/RSI=10/5、8/7、5/10。一次只改一组、固定其余参数；不得用样本外结果挑选参数，未达到原验收门槛不启用修订规则。

重要：第t根5分钟K线结束时计算信号，最早在随后允许交易的时刻成交，不能假设以信号K线的收盘价无条件成交。
上述回测门槛仅用于研究验收，不代表预计回报或收益保证。

## 十四、V1.0 参数速览

趋势：EMA20/EMA60，EMA差/ATR阈值±0.20，斜率/ATR阈值±0.05。
BOLL：(20,2)；KDJ：(9,3,3)；RSI：Wilder14，参考线30/70，持续评分边界45/55；ATR：Wilder14。
VWAP：每日重置，ATR归一化。
权重：EMA20 / BOLL20 / KDJ8 / RSI7 / VWAP25 / GRID20，合计100。
KDJ：交叉8分、持续4分；RSI：向上脱离30/向下脱离70计7分，低位持续恢复/高位持续衰减计4分，同方向不累加。
网格：0.9ATR；最小0.25%；至少3倍成本；最大允许0.90%。
BUY≥72；SELL≥68；TAKE_PROFIT评分≥60并有盈利目标；BUYBACK≥68、弱BEAR≥78；双向分差至少12。
BUY/BUYBACK额外要求RSI>30且较前一根上升；普通SELL额外要求RSI<70且较前一根下降；TAKE_PROFIT和强制风险退出不受RSI硬门槛否决。
信号：只能由已完成5分钟线确认。
单笔委托≤NAV8%；单ETF总仓位≤NAV65%；单笔计划风险≤NAV0.15%。
止损距离=max(1.5ATR,1.2g,0.4%价格)。
日内亏损-0.8%停止增仓，-1.2%强制降低可卖仓风险。
日内成交订单≤8笔。
根据T+0/T+1分别处理库存和回转交易。

最终原则：技术评分决定机会，已完成5分钟K线决定确认时点，状态机决定交易意图，风险和库存决定能否执行。下一阶段应使用具体ETF的历史数据、真实交易成本和合法库存约束进行回测验证。

## 十五、RSI 设计依据与参数性质

公式参考：TradingView，Relative Strength Index (RSI)，Wilder/RMA平滑与14周期计算说明。
[TradingView RSI](https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/)
使用限制参考：Fidelity，Relative Strength Index，30/70常用参考线，以及强趋势中可能长期超买/超卖的说明。
[Fidelity RSI](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/RSI)
查阅日期：2026-11-03。资料支持指标定义和使用限制，不证明本策略具有收益优势。将14周期用于5分钟线、8/7权重、45/55边界及第六部分过滤均为本策略的研究假设；未进行ETF历史回测，也未据真实成交成本优化参数。
