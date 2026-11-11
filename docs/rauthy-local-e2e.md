# Isolated Rauthy browser/API acceptance

This disposable topology pins Rauthy v0.37.2 and is localhost-only. It uses official `LOCAL_TEST=true` (embedded single-node Hiqlite and self-signed HTTPS); only HTTPS is published, bound to `127.0.0.1`. A separate `rauthy-data` volume persists Rauthy data only for the run. The default Keycloak configuration and API token validation are unchanged.

## Prerequisites and run

Install Podman Compose, Python 3.14/uv, repo development dependencies, and Chromium:

```sh
uv sync --group dev
uv run playwright install chromium
uv run python scripts/compose_rauthy_browser_e2e.py
```

Rauthy's discovery issuer is `https://localhost:<rauthy-port>/auth/v1/`; the runner uses the issuer and JWKS URI returned by its discovery document, including that path, for gateway validation.

The runner creates a random Compose project and free localhost ports, uses replay mode, starts the existing gateway and APIs, and prints the local Rauthy Admin UI URL. In a browser, accept the self-signed local certificate warning and use Rauthy's official v0.37.2 local-test admin instructions. Create these clients:

| Setting | Browser client | Device client |
| --- | --- | --- |
| Client ID | `a500-browser-client` | `a500-device-client` |
| Client type | Public, no secret | Public, no secret |
| Redirect URI | `http://localhost:<printed-gateway-port>/` | None |
| Allowed origin | `http://localhost:<printed-gateway-port>` | None |
| Grant | Authorization Code | Device Authorization |
| PKCE | Required, S256 | Not applicable |
| Signing algorithm | RS256 (not the EdDSA default) | RS256 (not the EdDSA default) |
| Default audience | `a500-api` | `a500-api` |
| Access-token lifetime | 300 seconds | 300 seconds |
| Scope | `openid` | `openid email` |

Create two enabled, disposable users: one for the browser flow and one for device authorization. Do not reuse real credentials. Enter the browser account only at the hidden prompt. The runner performs browser login, observes both tokens from the real token response, validates access-token issuer, audience, RS256, and five-minute lifetime, verifies a signal read and account snapshot create/read against the public API, and rejects the real ID token. It also creates a real trade-import preview as the browser user and checks that the device user cannot read or confirm it. Then it starts device authorization, prints the real verification URL and code, and waits for a human to approve the second user. That real access token must pass the public signal API, while the second subject must receive 404 for the browser subject's account snapshot and trade-import preview.

The API's successful responses verify access-token signatures through its existing Rauthy JWKS validator. Failure of either token path is an acceptance failure. The runner neither logs credentials/token nor bypasses validation. The admin API contract test uses a synthetic signed `admin` role solely to verify that this identity role cannot use Alice's snapshot ETag to update it; it is not evidence about Rauthy's real administrator. A separate real administrator login and public API attempt are still required: authenticate a real administrator account, then use its actual access token to attempt to read and update the browser user's subject-owned snapshot; both attempts must fail to disclose or modify the browser user's data.

## Cleanup and safety

The `finally` cleanup executes Compose `down --volumes --remove-orphans --rmi local` against only the random project. This removes its containers, network, database volumes (including Rauthy Hiqlite), and locally built project images. The downloaded pinned Rauthy image is retained. Ctrl-C follows the same cleanup. The local-test mode/demo admin are unsafe outside this disposable localhost setup. Re-run starts with fresh data and requires recreating the clients and users.

## Source note

Issue #221 references `docs/adr/0021-rauthy-identity-provider.md`, absent from this worktree. The issue descriptions and version-pinned official Rauthy research are used; this is a test fixture, not a production IdP decision.
