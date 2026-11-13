# Isolated Rauthy browser/device/API acceptance

This disposable topology pins Rauthy v0.37.2 and is localhost-only. It runs Rauthy with a per-run bootstrap directory, private Hiqlite secrets, a per-run generated initial administrator password, and cleartext HTTP bound only to `127.0.0.1`. The bootstrap override is private to the run, and the explicit initial password prevents Rauthy from generating and logging a default administrator password. A separate `rauthy-data` volume persists Rauthy data only for the run. The default Keycloak configuration and API token validation are unchanged. Cleartext HTTP is strictly for this isolated local acceptance, not production use.

## Prerequisites and run

Install Podman Compose, Python 3.14/uv, repo development dependencies, and Chromium:

```sh
uv sync --group dev --default-index https://mirrors.aliyun.com/pypi/simple/
uv run playwright install chromium
uv run python scripts/compose_rauthy_browser_e2e.py
```

The runner rebuilds the current backend wheel before building the Compose services, so the container tests the checked-out source rather than a stale wheel in `dist/`.

Rauthy's discovery issuer is `http://localhost:<rauthy-port>/auth/v1/`; the runner uses the issuer and JWKS URI returned by its discovery document, including that path, for gateway validation. Issuer, browser PKCE, device authorization, and discovery traffic use real HTTP; the acceptance does not count HTTPS with certificate verification disabled as HTTP.

The runner creates a random Compose project, free localhost ports, and disposable Rauthy users and clients from a temporary bootstrap directory. No manual Rauthy provisioning or email activation is needed. It uses replay mode and starts the gateway and APIs with the Rauthy discovery issuer and JWKS URI. Three accounts are provisioned: browser user, device user, and identity administrator. Credentials are generated per run and never printed.

The runner drives the real browser authorization-code flow with PKCE/S256 and the real device-authorization flow in a browser, obtaining each user's access token from Rauthy. It checks token issuer, audience, signature algorithm, five-minute lifetime, and that both tokens can call the public signal API. It verifies each subject's separately valued snapshot, rejects the real ID token, and checks the second subject cannot read or confirm the browser subject's trade-import preview. The actual identity-administrator token is also denied access to the browser user's subject-owned preview.

After ordinary Rauthy stop/start, the runner verifies that the same Rauthy container and same named `/app/data` volume are in use, then logs in again through both separate clients and verifies the original subjects and their isolated account data remain usable.

The successful API requests verify access-token signatures through the existing Rauthy JWKS validator. Failure of either token path or any cross-subject/admin isolation check is an acceptance failure. The runner does not log credentials or tokens and does not bypass validation.

## User disable, refresh, and access-token expiry

The browser client receives a five-minute access token (`access_token_lifetime: 300`) and a refresh credential from the real Rauthy authorization-code flow. In the isolated run, the Rauthy administrator API disables only the disposable browser user; the runner confirms the disabled state, attempts a fresh browser login, and submits the already-issued refresh credential. Both authentication operations must be rejected. The credential and token values are never printed.

The runner then calls the public API with the access token issued before disablement. It must still succeed while that JWT is unexpired because the API validates its signature, issuer, audience, and expiration locally; it does not query Rauthy for every request or implement online revocation. After the token's actual `exp` time, the same API call must return `401`. User disablement therefore prevents new authentication/renewal but does not instantly revoke already-issued access tokens; the residual acceptance window lasts until their expiry (up to five minutes from issuance).

This is a disposable local acceptance fixture, not evidence from a production identity deployment. The isolated local Podman Compose run is sufficient acceptance; no separately deployed environment is required.

## 用户准入、邮箱变更与邮件捕获

隔离 Rauthy 使用 `OPEN_USER_REG=false`，测试账号由一次性 bootstrap 创建并已启用、已验证邮箱，不会发送账户激活邮件。该 bootstrap 用于自动化验收，不等同于人工管理员在 Rauthy 管理界面创建用户；真实环境必须由管理员创建并审批账号，禁止开放自助注册。

用户邮箱变更验收会修改一次性浏览器用户邮箱，再使用新邮箱登录；runner 比对变更前后的 `sub`，并以新 token 读取先前创建的账户快照。另一用户及身份管理员均不能读取或确认浏览器用户的交易导入预览。系统按稳定的身份主体 ID 归属业务数据，不按邮箱转移数据。

runner 实际提交密码恢复请求，轮询 Mailpit API 取得该一次性用户的邮件正文，从本机 Rauthy 链接完成密码重设，再用新密码执行真实浏览器登录，并使用新 access token 读取原账户快照。恢复链接、恢复令牌、密码和 access token 均只在进程内存中处理，不输出到 runner 日志；测试只接受 issuer 主机为 localhost 的恢复链接。Mailpit 已启动或只收到邮件仍不构成恢复通过。

## 当前验收边界

runner 自动验证的是隔离 Compose 中的 Rauthy，而不是部署环境。用户邮箱变更和 Mailpit 密码恢复会实际操作 Rauthy，并通过浏览器重新认证及业务 API 验证主体连续性。用户自助注册关闭、真实管理员创建／审批，以及 localhost Passkey 都需要分别完成相应的真实验收后，才能勾选 issue #226 对应标准。
The runner creates a disposable enabled, email-verified account and separate public clients for browser authorization and the independent device-code grant in a private temporary bootstrap directory. Rauthy v0.37.2's bootstrap client schema does not contain `default_aud`; the runner bootstraps a temporary API key restricted to client updates, then sets and reads back `default_aud: ["a500-api"]` through Rauthy's supported client API. The device client enables only `urn:ietf:params:oauth:grant-type:device_code`, signs access and ID tokens with RS256, and issues five-minute access tokens.
Browser automation failures are reported with a fixed message; Playwright diagnostics, verification URLs, test credentials, and page content are not included in the raised error.

| Setting               | Value                                      |
| --------------------- | ------------------------------------------ |
| Client ID             | `a500-browser-client`                      |
| Client type           | Public, no secret                          |
| Redirect URI          | `http://localhost:<printed-gateway-port>/` |
| Allowed origin        | `http://localhost:<printed-gateway-port>`  |
| Grant                 | Authorization Code                         |
| PKCE                  | Required, S256                             |
| Signing algorithm     | RS256 (not the EdDSA default)              |
| Default audience      | `a500-api`                                 |
| Access-token lifetime | 300 seconds                                |

The runner performs the PKCE browser-login acceptance, then exercises a separate device authorization context without reusing the A500 browser session. The device flow checks pending, early-poll `slow_down`, approval, denial, and natural expiry. It verifies issuer, audience, RS256, and five-minute token lifetime, then accesses signals, writes and reads an account snapshot, and verifies SSE through the public gateway. Credentials and tokens are generated per run and never logged; temporary bootstrap files are removed after Compose cleanup.

## Redacted acceptance evidence

Previously recorded HTTPS-only evidence is historical and does not satisfy the current local HTTP acceptance.

## Cleanup and safety

`finally` 清理只针对本次随机 Compose 项目：删除容器、网络、数据库卷（包括 Rauthy Hiqlite）和本地构建镜像。若 Mailpit 或固定版本 Rauthy 镜像由本次测试拉取，测试结束后删除；仅保留运行前已存在的镜像。清理后检查容器、卷和镜像；残留或清理失败会使测试失败。

## Source note

Issue #221 references `docs/adr/0021-rauthy-identity-provider.md`, absent from this worktree. The issue descriptions and version-pinned official Rauthy research are used; this is a test fixture, not a production IdP decision.
