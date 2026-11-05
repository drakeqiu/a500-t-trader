# 账户快照 API

`GET /v1/account-snapshot` 返回当前认证主体的当前账户快照；首次写入前返回 `404`。快照包含 `as_of`、`holdings`、`protected_floor`、`sellable`、`cost_basis`、`available_cash`、服务器写入时间 `updated_at` 和 `user-entered; not broker-verified` 分类。响应带有代表完整当前状态的强 `ETag`。

`PUT /v1/account-snapshot` 是完整替换，不是部分更新。请求体必须且只能提供 `as_of`、`holdings`、`protected_floor`、`sellable`、`cost_basis`、`available_cash` 六个字段；缺失或未知字段、任意位置的 `null`、以及不符合字段类型的值均拒绝。`as_of` 必须是带 `Z` 或 `±HH:MM` 时区偏移的 RFC 3339 日期时间，不限制历史日期。金额必须是有限、非负的十进制字符串。证券数量必须是非负整数（布尔值不算整数）；`holdings`、`protected_floor`、`sellable` 的证券 key 集合必须相同，且 `protected_floor <= holdings`、`sellable <= holdings - protected_floor`。`cost_basis` 必须为对象，key 经证券代码标准化后须为 holdings 的子集，金额也必须是有限非负十进制字符串；所有证券映射标准化后发生重复时拒绝。

格式错误或非标准 JSON（包括 `NaN`、`Infinity`）返回 `400 application/problem+json`；合法 JSON 但不符合快照契约返回 `422 application/problem+json`。错误响应的 `errors` 数组提供稳定的 `path` 和 `code`，例如 `{"path":"holdings.510300","code":"invalid_quantity"}`。校验失败不会修改当前快照或追加历史记录。成功响应保留规范化证券 key，并包含 `user-entered; not broker-verified` classification。

首次创建必须带 `If-None-Match: *`，成功返回 `201` 和 `ETag`。资源已存在时必须带当前响应的 `If-Match`；匹配时替换成功返回 `200` 和新的 `ETag`。已有资源但缺少 `If-Match`、或新建时缺少 `If-None-Match`，返回 `428`；当前资源缺失/旧 ETag 或创建前置条件不匹配，返回 `412`。每次成功写入都在与当前状态同一个 SQLite 事务中追加内部历史记录；失败写入不产生历史。没有删除或历史查询路由。

身份只来自 OIDC Bearer JWT 的已验证 `sub`；不接受客户端用户或账户标识作为 ownership。

账户 API 与其他受保护 API 共用 OIDC 配置，并在应用内验证 JWT 签名、issuer、audience、有效期及必需的 `exp`、`iat`、`iss`、`aud`、`sub` 声明。共享 audience 固定为 `a500-api`；旧 `a500-account-api` audience 不被接受。缺少任何 OIDC 配置时生产应用拒绝启动，不会降级为匿名访问：

```sh
A500_OIDC_ISSUER=https://identity.example.internal/
A500_OIDC_AUDIENCE=a500-api
A500_OIDC_JWKS_URI=https://identity.example.internal/.well-known/jwks.json
A500_ACCOUNT_DATABASE=/var/lib/a500/accounts.sqlite3
```

未提供有效 OIDC 配置或 AmazingData 行情提供方配置时，生产应用拒绝启动。生产部署需将 SQLite 数据库放在持久化、受限访问的存储中；`a500-account-api` 使用 `AMAZINGDATA_USERNAME`、`AMAZINGDATA_PASSWORD`、`AMAZINGDATA_HOST` 和 `AMAZINGDATA_PORT` 配置行情查询服务。测试可通过 `create_app(database, authenticator, market_data=...)` 使用临时数据库、确定性身份验证替身及可控行情服务。

## 当前净资产

`GET /v1/account-nav` 根据当前认证主体的账户快照与行情计算当前 NAV；不接受用户或账户标识参数。接口使用账户 API 的 OIDC Bearer 认证，身份与数据归属仅来自已验证的 `sub`。无账户快照时返回 `404`，响应标记 `status: unavailable` 且 `nav: null`。

计算公式为 `available_cash + Σ(持仓数量 × 有效行情价)`，货币为 CNY。保护底仓、可卖数量和成本基础不参与估值。零数量持仓估值为零，不要求行情。所有金额使用精确十进制运算并以十进制字符串返回；不逐项舍入或强制保留两位小数。

行情通过注入的请求/响应 `MarketDataService.get_snapshot(symbol)` 按每项非零持仓标的查询；不依赖推送行情的共享缓存。账户快照 `as_of` 及每项必需行情的 `timestamp` 必须不晚于计算时刻且不超过 30 分钟；恰好 30 分钟有效。未来或过期时点、缺失报价、报价能力不可用，均使完整 NAV 不可用。收盘后的报价超过时效上限后不可用于当前 NAV。

若存在账户快照但无法完整估值，返回 `200`，`status` 为 `unavailable`、`nav` 为 `null`，并以 `reason` 标示原因；`holdings` 为空，不暴露部分估值明细或部分合计。完整估值时 `status` 为 `available`，`nav` 为现金与全部非零持仓市值的合计。响应包含 `currency`、`available_cash`、账户 `as_of`、服务器 `calculated_at`、每项持仓的 `symbol`、`quantity`、`last_price`、`market_value` 和行情 `as_of`，并标记 `user-entered; not broker-verified`。响应设置 `Cache-Control: no-store`。

可用响应示例：

```json
{
  "status": "available",
  "currency": "CNY",
  "nav": "1234.567",
  "available_cash": "500.00",
  "account_snapshot_as_of": "2026-11-05T10:00:00+08:00",
  "calculated_at": "2026-11-05T10:05:00+00:00",
  "holdings": [
    {
      "symbol": "510300",
      "quantity": 100,
      "last_price": "7.34567",
      "market_value": "734.56700",
      "as_of": "2026-11-05T10:04:58+08:00"
    }
  ],
  "classification": "user-entered; not broker-verified"
}
```

不可用响应示例：

```json
{
  "status": "unavailable",
  "currency": "CNY",
  "nav": null,
  "available_cash": "500.00",
  "account_snapshot_as_of": "2026-11-05T10:00:00+08:00",
  "calculated_at": "2026-11-05T10:35:01+00:00",
  "holdings": [],
  "classification": "user-entered; not broker-verified",
  "reason": "quote_stale"
}
```

---

## 成交文件预览

`POST /v1/trade-imports/previews` 接受 multipart 字段 `file`，只支持现有银河证券 GB18030 CSV 表头。请求必须使用与账户快照相同的 Bearer 认证；所有权仅来自验证后的 OIDC `sub`。上传上限为 5 MiB、10,000 条记录，超限返回 `413`，不支持的格式返回 `422`。

预览响应包含 `batch_id`、`valid`、`new`、`duplicates`、`errors` 和脱敏 `error_details`。预览不会写入成交记录或更新账户快照；仅将解析后的成交暂存 15 分钟，不保留原始上传文件。无效行的错误只包含 CSV 行号、字段和固定原因，不包含原始行内容、股东账户号或资金余额。导入成交独立存储，并标记为用户提供、未经系统核实。

通过 `POST /v1/trade-imports/previews/{batch_id}/confirm` 显式确认预览。主体必须与创建预览的已验证 `sub` 相同；其他主体及不存在或已过期的未确认批次返回 `404`。含校验错误的预览返回 `409` 且不写入。成功响应含 `valid`、`new`、`duplicates`、`errors`、`inserted_ids` 和 `confirmed`；同一主体重复确认返回首次确认的相同结果，不会重复插入。确认成功后立即清除暂存的解析记录。

`GET /v1/trade-imports/previews/{batch_id}` 返回当前主体可见批次的持久化预览结果：`batch_id`、预览汇总、脱敏 `error_details`，以及 `confirmed`（未确认时为 `null`，确认后为首次确认响应）。只有创建批次的已验证 `sub` 可读取；其他主体、不存在或已过期的未确认批次统一返回 `404`。已确认批次的结果在解析记录清除后仍可查询及重复确认。响应不包含原始 CSV 或其他主体的记录。
