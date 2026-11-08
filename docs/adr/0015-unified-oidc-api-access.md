# Protect web access and public APIs with OIDC

**Status:** Partially superseded by [ADR-0016](0016-keycloak-device-flow-without-frontend.md). Shared OIDC API validation and audience decisions remain in effect; the browser-client decision does not.

The account API and signal API validate OIDC bearer JWT signatures, issuer, shared audience, and validity. The identity provider controls which users may authenticate; the application provides no self-registration. Authenticated users share market data and signals, while account snapshots and imported trades are isolated by the verified token `sub`. `/healthz` is internal-only, and API documentation and OpenAPI schema require a valid Bearer token. The legacy `a500-account-api` audience is not accepted.

The shared audience is `a500-api`, configured for each API through `OIDC_AUDIENCE`. Callers obtain Bearer access tokens directly from the trusted identity provider.
