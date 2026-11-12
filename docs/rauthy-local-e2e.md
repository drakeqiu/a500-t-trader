# Isolated Rauthy browser/API acceptance

This disposable topology pins Rauthy v0.37.2 and is localhost-only. It runs Rauthy with `LOCAL_TEST=false` so an empty, project-specific Hiqlite database imports the temporary bootstrap users, clients, and API key. Only HTTPS is published, bound to `127.0.0.1`; certificates are self-signed. The default identity-provider configuration and API token validation are unchanged.

## Prerequisites and run

Install Podman Compose, Python 3.14/uv, repo development dependencies, and Chromium:

```sh
uv sync --group dev
uv run playwright install chromium
uv run python scripts/compose_rauthy_browser_e2e.py
```

Rauthy's discovery issuer is `https://localhost:<rauthy-port>/auth/v1/`; the runner uses the issuer and JWKS URI returned by its discovery document, including that path, for gateway validation.

The runner creates a random Compose project, free localhost ports, and disposable Rauthy users and clients from a temporary bootstrap directory. No manual Rauthy provisioning or email activation is needed. It uses replay mode and starts the gateway and APIs with the Rauthy discovery issuer and JWKS URI. Three accounts are provisioned: browser user, device user, and identity administrator. Credentials are generated per run and never printed.

The runner drives the real browser authorization-code flow with PKCE/S256 and the real device-authorization flow in a browser, obtaining each user's access token from Rauthy. It checks token issuer, audience, signature algorithm, five-minute lifetime, and that both tokens can call the public signal API. It verifies user-owned snapshot creation/read, rejects the real ID token, and checks the second subject cannot read or confirm the browser subject's snapshot or trade-import preview. The actual identity-administrator token is also denied access to the browser user's subject-owned data.

The successful API requests verify access-token signatures through the existing Rauthy JWKS validator. Failure of either token path or any cross-subject/admin isolation check is an acceptance failure. The runner does not log credentials or tokens and does not bypass validation.

## Cleanup and safety

The `finally` cleanup executes Compose `down --volumes --remove-orphans --rmi local` against only the random project. This removes its containers, network, database volumes (including Rauthy Hiqlite), and locally built project images. The downloaded pinned Rauthy image is retained. Every run starts with fresh data and recreates all clients and users; no prior test data is migrated.

## Source note

Issue #221 references `docs/adr/0021-rauthy-identity-provider.md`, absent from this worktree. The issue descriptions and version-pinned official Rauthy research are used; this is a test fixture, not a production IdP decision.
