## A500 Trader 前端

Next.js App Router 工作台，直接调用仓库现有 Python REST API。前端不实现业务 API，也不承担 OAuth token broker。

### 本地运行

```bash
bun install
bun run dev
```

### 服务配置

在启动 Next.js 前设置以下公开配置：

```bash
NEXT_PUBLIC_A500_API_BASE_URL=https://api.example.internal
NEXT_PUBLIC_A500_OIDC_ISSUER=https://identity.example.internal/realms/a500
# 可选；默认 a500-device-client
NEXT_PUBLIC_A500_OIDC_CLIENT_ID=a500-device-client
```

OIDC issuer discovery 必须提供设备授权和 token endpoint。前端按 OAuth 2.0 Device Authorization Grant 展示用户码并按 IdP 间隔轮询；访问令牌仅保存在当前页面内存，刷新页面后需重新授权。绝不把令牌写入 localStorage、URL 或日志。

浏览器需能访问 OIDC issuer 和 API origin；API 与 IdP 必须允许前端部署来源的 CORS 请求。部署方负责提供正确 issuer、API 地址与跨域策略，不要将任何客户端密钥写入 `NEXT_PUBLIC_*` 变量。

### 首页与登录

未授权时首页显示 OIDC 设备码登录页；完成身份提供方验证后自动进入工作台。页面不收集密码，设备代码仅用于当前授权流程。

授权后首页提供：

- 读取 `/v1/signals`，并订阅 `/v1/signals/stream` 展示已配置标的当前状态；信号仅供参考，不代表交易执行。
- 读取当前认证主体的 `/v1/account-nav`；账户快照和净资产明确标记为用户录入、未经券商核验。
- 银河证券 CSV 导入按预览、显式确认两步调用现有 API；原始文件不由前端持久化。
- 账户快照编辑等其他已存在 API 尚未在此首页提供操作入口。

本地开发地址默认为 [http://localhost:3000](http://localhost:3000)。

### 测试

- `bun run test` 运行单元测试。
- `bun run test:e2e` 运行 Playwright 端到端测试；首次运行先执行 `bunx playwright install chromium`。
