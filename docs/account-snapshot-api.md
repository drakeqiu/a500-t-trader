# 账户快照读取 API

`GET /v1/account-snapshot` 返回当前认证主体的账户快照。服务只从 OIDC Bearer JWT 的已验证 `sub` 读取所有权，不接受请求参数或请求体中的用户/账户标识。尚未录入快照时返回 `404`；有快照时返回 `as_of`、持仓映射、保护底仓、可卖数量、成本、可用资金、服务器写入时间 `updated_at` 及 `user-entered; not broker-verified` 分类。响应带有代表当前内容的强 `ETag`。

配置 OIDC 身份提供方的 issuer、受众及可信 JWKS 地址：

```sh
A500_OIDC_ISSUER=https://identity.example.internal/
A500_OIDC_AUDIENCE=a500-account-api
A500_OIDC_JWKS_URI=https://identity.example.internal/.well-known/jwks.json
A500_ACCOUNT_DATABASE=/var/lib/a500/accounts.sqlite3
```

未提供有效 OIDC 配置时应用拒绝启动。生产部署需将 SQLite 数据库放在持久化、受限访问的存储中；测试可通过 `create_app(database, authenticator)` 使用临时数据库与确定性身份验证替身。

账户状态时间 `as_of` 由录入数据携带，可为历史时点；`updated_at` 由服务器保存快照时生成。该数据是用户录入状态，不代表券商核验。此读取 API 不提供写入、历史查询或删除端点。
