# Isolated Rauthy browser/device/API acceptance

This disposable topology pins Rauthy v0.37.2 and is localhost-only. It runs Rauthy with a per-run bootstrap directory, private Hiqlite secrets, a per-run generated initial administrator password, and cleartext HTTP bound only to `127.0.0.1`. The bootstrap override is private to the run, and the explicit initial password prevents Rauthy from generating and logging a default administrator password. A separate `rauthy-data` volume persists Rauthy data only for the run. The existing API JWT validation boundaries remain unchanged. Cleartext HTTP is strictly for this isolated local acceptance, not production use.

## Prerequisites and run

Install Podman Compose, Python 3.14/uv, repo development dependencies, and Chromium:

```sh
uv sync --group dev --default-index https://mirrors.aliyun.com/pypi/simple/
uv run playwright install chromium
uv run python scripts/compose_rauthy_browser_e2e.py
```

The runner rebuilds the current backend wheel before building the Compose services, so the container tests the checked-out source rather than a stale wheel in `dist/`.
`scripts/start-local.py` checks the configured `OIDC_ISSUER` discovery document and the configured gateway port, so local overrides use the same readiness checks as their Compose endpoints. Before browser login, the Rauthy runner also waits for the account-snapshot route to answer through the public gateway, avoiding requests during account API startup.

Rauthy's discovery issuer is `http://localhost:<rauthy-port>/auth/v1/`; the runner uses the issuer and JWKS URI returned by its discovery document, including that path, for gateway validation. Issuer, browser PKCE, device authorization, and discovery traffic use real HTTP; the acceptance does not count HTTPS with certificate verification disabled as HTTP.

The runner creates a random Compose project, free localhost ports, and disposable Rauthy users and clients from a temporary bootstrap directory. No manual Rauthy provisioning or email activation is needed. It uses replay mode and starts the gateway and APIs with the Rauthy discovery issuer and JWKS URI. Three accounts are provisioned: browser user, device user, and identity administrator. Credentials are generated per run and never printed.

The runner drives the real browser authorization-code flow with PKCE/S256 and the real device-authorization flow in a browser, obtaining each user's access token from Rauthy. It checks token issuer, audience, signature algorithm, five-minute lifetime, and that both tokens can call the public signal API. It verifies each subject's separately valued snapshot, rejects the real ID token, and checks the second subject cannot read or confirm the browser subject's trade-import preview. The actual identity-administrator token is also denied access to the browser user's subject-owned preview.

After ordinary Rauthy stop/start, the runner verifies that the same named `/app/data` volume is mounted, then logs in again through both separate clients and verifies the original subjects and isolated account data remain usable.

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

runner 自动验证的是隔离 Compose 中的 Rauthy，不代表生产部署。#226 的用户准入、邮箱变更、localhost 虚拟 Passkey 和历史 Mailpit 恢复证据已分别记录在该 issue；其中邮件恢复不属于当前切换门槛，localhost Passkey 不外推到远程 HTTP WebAuthn。
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

## #231 切换准入审核（2026-11-13）

**结论：GO，允许开始 #232 迁移实施；这不是切换已完成、生产部署已验收或旧数据可清理的授权。** 当前有效的剩余核心门槛均有通过证据；失败、缺证、未执行：无。该结论供 #231 集成负责人记录处理，本文件变更不关闭 issue。

| 有效门槛                                                   | 状态与证据                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 浏览器 PKCE 与真实业务 API                                 | **通过**：[#223](https://github.com/drakeqiu/a500-t-trader/issues/223) 记录真实浏览器登录和业务 API 访问；[#228 最终运行](https://github.com/drakeqiu/a500-t-trader/issues/228#issuecomment-6852074539) 重新验证 HTTP 候选下真实浏览器 token 调用 API。                                            |
| 独立设备授权及 pending/slow_down/拒绝/过期                 | **通过**：[#224](https://github.com/drakeqiu/a500-t-trader/issues/224) 记录真实设备协议边界及 API；#228 又验证 HTTP 候选设备 token API 路径。                                                                                                                                                      |
| JWT 拒绝边界、token 用途、主体与管理员隔离                 | **通过**：[#225](https://github.com/drakeqiu/a500-t-trader/issues/225) 记录签名/issuer/audience/ID token 拒绝及跨主体隔离；#228 验证 issuer/JWKS 路径、token subject、跨用户及管理主体隔离。                                                                                                       |
| 用户准入、邮箱变更、密码、可选 localhost Passkey           | **通过**：[#226](https://github.com/drakeqiu/a500-t-trader/issues/226) 保留已完成证据；Passkey 使用 localhost 虚拟 WebAuthn，不证明远程 HTTP WebAuthn 可用。Mailpit 恢复仅为历史能力证据，不是当前门槛。                                                                                           |
| 停用、续期拒绝及到期边界                                   | **通过**：[#227](https://github.com/drakeqiu/a500-t-trader/issues/227) 记录停用后新登录/refresh 被拒、旧令牌到期前仍能访问且到期后拒绝。API 本地 JWT 校验意味着最多 5 分钟残余窗口，不提供即时撤权。                                                                                               |
| HTTP 候选真实浏览器/设备 API、issuer/JWKS 与普通重启持久化 | **通过**：[#228 最终运行](https://github.com/drakeqiu/a500-t-trader/issues/228#issuecomment-6852074539) 报告本机隔离 Rauthy/Compose HTTP 候选完整 runner exit 0、25 个聚焦 pytest 通过，两个真实客户端均访问 API，身份和配置在同一独立持久卷正常重启后仍可用；候选资源已清理，未触及既有业务数据。 |

[#222](https://github.com/drakeqiu/a500-t-trader/issues/222) 是 Keycloak 本地验收历史基线，不作为 Rauthy 通过证据；Rauthy 能力以 #223–#228 的具体运行记录为准。

**范围取消/N/A（不算通过）：** HTTPS、证书/CA/TLS，LAN/VPN/内网或公网访问隔离；MFA/应急账号；外部/加密 SMTP 与开户/凭据恢复邮件；备份/灾难恢复；资源收益及性能数值。HTTP 接受凭据、授权码、会话、令牌被监听、篡改或窃取并导致账号接管的风险，不声称具备传输保护或安全的公网认证部署。真实浏览器若因非安全上下文、混合内容等限制不能完成登录仍须报告失败，不能借范围取消标为通过。

[#229](https://github.com/drakeqiu/a500-t-trader/issues/229) 的 not planned 关闭表示取消灾备/恢复验收；[#230](https://github.com/drakeqiu/a500-t-trader/issues/230) 的 not planned 关闭表示取消资源收益验收。两者均非通过证据，也不阻塞准入；保留的 Mailpit 历史恢复证据不等于灾难恢复保证。

Issue #232 migrates active local and CI identity-provider paths to Rauthy while preserving API issuer/signature/audience/token-use validation and subject isolation. It does not remove, migrate, or authorize cleanup of any pre-existing identity or business-data volumes. This isolated runner only uses a fresh random project and its disposable resources.

## Redacted acceptance evidence

Previously recorded HTTPS-only evidence is historical and does not satisfy the current local HTTP acceptance.

## Cleanup and safety

`finally` 清理只针对本次随机 Compose 项目：删除容器、网络、数据库卷（包括 Rauthy Hiqlite）和本地构建镜像。若 Mailpit 或固定版本 Rauthy 镜像由本次测试拉取，测试结束后删除；仅保留运行前已存在的镜像。清理后检查容器、卷和镜像；残留或清理失败会使测试失败。

## Source note

This fixture is an isolated acceptance runner, not a production deployment. Historical decisions and evidence remain in the ADR and issue records; no additional ADR was created for the migration.
