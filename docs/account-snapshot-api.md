# 账户快照 API

`GET /v1/account-snapshot` 返回当前认证主体的当前账户快照；首次写入前返回 `404`。快照包含 `as_of`、`holdings`、`protected_floor`、`sellable`、`cost_basis`、`available_cash`、服务器写入时间 `updated_at` 和 `user-entered; not broker-verified` 分类。响应带有代表完整当前状态的强 `ETag`。

`PUT /v1/account-snapshot` 是完整替换，不是部分更新。请求体必须且只能提供 `as_of`、`holdings`、`protected_floor`、`sellable`、`cost_basis`、`available_cash` 六个字段；缺失或未知字段、任意位置的 `null`、以及不符合字段类型的值均拒绝。`as_of` 必须是带 `Z` 或 `±HH:MM` 时区偏移的 RFC 3339 日期时间，不限制历史日期。金额必须是有限、非负的十进制字符串。证券数量必须是非负整数（布尔值不算整数）；`holdings`、`protected_floor`、`sellable` 的证券 key 集合必须相同，且 `protected_floor <= holdings`、`sellable <= holdings - protected_floor`。`cost_basis` 必须为对象，key 经证券代码标准化后须为 holdings 的子集，金额也必须是有限非负十进制字符串；所有证券映射标准化后发生重复时拒绝。

格式错误或非标准 JSON（包括 `NaN`、`Infinity`）返回 `400 application/problem+json`；合法 JSON 但不符合快照契约返回 `422 application/problem+json`。错误响应的 `errors` 数组提供稳定的 `path` 和 `code`，例如 `{"path":"holdings.510300","code":"invalid_quantity"}`。校验失败不会修改当前快照或追加历史记录。成功响应保留规范化证券 key，并包含 `user-entered; not broker-verified` classification。

首次创建必须带 `If-None-Match: *`，成功返回 `201` 和 `ETag`。资源已存在时必须带当前响应的 `If-Match`；匹配时替换成功返回 `200` 和新的 `ETag`。已有资源但缺少 `If-Match`、或新建时缺少 `If-None-Match`，返回 `428`；当前资源缺失/旧 ETag 或创建前置条件不匹配，返回 `412`。每次成功写入都在与当前状态同一个 SQLite 事务中追加内部历史记录；失败写入不产生历史。没有删除或历史查询路由。

身份只来自 OIDC Bearer JWT 的已验证 `sub`；不接受客户端用户或账户标识作为 ownership。

配置 OIDC 身份提供方的 issuer、受众及可信 JWKS 地址：

```sh
A500_OIDC_ISSUER=https://identity.example.internal/
A500_OIDC_AUDIENCE=a500-account-api
A500_OIDC_JWKS_URI=https://identity.example.internal/.well-known/jwks.json
A500_ACCOUNT_DATABASE=/var/lib/a500/accounts.sqlite3
```

未提供有效 OIDC 配置时应用拒绝启动。生产部署需将 SQLite 数据库放在持久化、受限访问的存储中；测试可通过 `create_app(database, authenticator)` 使用临时数据库与确定性身份验证替身。

## 成交文件预览

`POST /v1/trade-imports/previews` 接受 multipart 字段 `file`，只支持现有银河证券 GB18030 CSV 表头。请求必须使用与账户快照相同的 Bearer 认证；所有权仅来自验证后的 OIDC `sub`。上传上限为 5 MiB、10,000 条记录，超限返回 `413`，不支持的格式返回 `422`。

预览响应包含 `batch_id`、`valid`、`new`、`duplicates`、`errors` 和脱敏 `error_details`。预览不会写入成交记录或更新账户快照；仅将解析后的成交暂存 15 分钟，不保留原始上传文件。无效行的错误只包含 CSV 行号、字段和固定原因，不包含原始行内容、股东账户号或资金余额。导入成交独立存储，并标记为用户提供、未经系统核实。
