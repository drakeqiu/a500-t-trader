# Isolated Rauthy browser/device/API acceptance

This disposable topology pins Rauthy v0.37.2 and is localhost-only. It runs Rauthy with a per-run bootstrap directory, private Hiqlite secrets, a per-run generated initial administrator password, and generated self-signed HTTPS; only HTTPS is published, bound to `127.0.0.1`. The bootstrap override is private to the run, and the explicit initial password prevents Rauthy from generating and logging a default administrator password. A separate `rauthy-data` volume persists Rauthy data only for the run. The default Keycloak configuration and API token validation are unchanged.

## Prerequisites and run

Install Podman Compose, Python 3.14/uv, repo development dependencies, and Chromium:

```sh
uv sync --group dev
uv run playwright install chromium
uv run python scripts/compose_rauthy_browser_e2e.py
```

The runner rebuilds the current backend wheel before building the Compose services, so the container tests the checked-out source rather than a stale wheel in `dist/`.

Rauthy's discovery issuer is `https://localhost:<rauthy-port>/auth/v1/`; the runner uses the issuer and JWKS URI returned by its discovery document, including that path, for gateway validation.

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

A real Podman Compose run against Rauthy v0.37.2 completed in 505.87 seconds, including the build and natural device-code expiry. Sanitized runner output:

```text
Verified PKCE browser login, token claims, protected signal read, snapshot write/read.
Device protocol: authorization_pending.
Device protocol: slow_down; respecting updated poll interval.
Device protocol: approved; RS256, default audience and five-minute token verified.
Device protocol: user denial terminated without a token.
Device protocol: waiting for natural expiry (300s).
Device protocol: expiry terminated without a token.
```

The approved device token accessed the protected signal API, wrote and read an account snapshot, and passed the authenticated SSE acceptance through the public gateway. The client API read back `a500-api` as the default audience. The denial and expiry paths returned no usable token. The run used a per-run generated administrator password; the runner emitted no credentials, tokens, verification code, or password. Compose teardown emitted a rootless-network permission warning; a scoped post-run Podman audit found no matching containers, images, volumes, or networks.

## Cleanup and safety

The `finally` cleanup executes Compose `down --volumes --remove-orphans --rmi local` against only the random project. This removes its containers, network, database volumes (including Rauthy Hiqlite), and locally built project images. The downloaded pinned Rauthy image is retained. Ctrl-C follows the same cleanup. The local-test mode/demo admin are unsafe outside this disposable localhost setup. Re-run starts with fresh data and requires recreating the client and user.

## Source note

Issue #221 references `docs/adr/0021-rauthy-identity-provider.md`, absent from this worktree. The issue descriptions and version-pinned official Rauthy research are used; this is a test fixture, not a production IdP decision.
