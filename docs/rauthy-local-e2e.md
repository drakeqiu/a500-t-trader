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

The runner creates a random Compose project and free localhost ports, uses replay mode, starts the existing gateway and APIs, and prints the local Rauthy Admin UI URL. In a browser, accept the self-signed local certificate warning and use Rauthy's official v0.37.2 local-test admin instructions. Create a public client with these exact values:

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
| Scope                 | `openid`                                   |

Create an enabled disposable user. Do not reuse real credentials. Enter the account only at the hidden prompt. The runner performs real browser login, observes the access token on an authenticated request through the gateway, checks issuer, audience, RS256, and exact five-minute lifetime; then checks protected signal read and account snapshot create/read using the same public gateway and business APIs. The API's successful responses verify the token signature through its existing Rauthy JWKS validator. The runner neither logs credentials/token nor bypasses validation.

## Cleanup and safety

The `finally` cleanup executes Compose `down --volumes --remove-orphans --rmi local` against only the random project. This removes its containers, network, database volumes (including Rauthy Hiqlite), and locally built project images. The downloaded pinned Rauthy image is retained. Ctrl-C follows the same cleanup. The local-test mode/demo admin are unsafe outside this disposable localhost setup. Re-run starts with fresh data and requires recreating the client and user.

## Source note

Issue #221 references `docs/adr/0021-rauthy-identity-provider.md`, absent from this worktree. The issue descriptions and version-pinned official Rauthy research are used; this is a test fixture, not a production IdP decision.
