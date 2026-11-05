# 账户快照 API

`GET /v1/account-snapshot` 返回当前认证主体的当前账户快照；首次写入前返回 `404`。快照包含 `as_of`、`holdings`、`protected_floor`、`sellable`、`cost_basis`、`available_cash`、服务器写入时间 `updated_at` 和 `user-entered; not broker-verified` 分类。响应带有代表完整当前状态的强 `ETag`。

`PUT /v1/account-snapshot` 是完整替换，不是部分更新。请求体必须提供全部快照字段；`as_of` 必须包含时区，金额使用有限、非负十进制字符串，数量为非负整数。允许空 `holdings`，它表示现金或清仓状态而非删除。

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
