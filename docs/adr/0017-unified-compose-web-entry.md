# Use one Compose gateway for the frontend and business APIs

**Status:** Accepted design; implemented in the #232 candidate. The historical Keycloak provider selection is superseded by Rauthy; the shared gateway and build-time frontend configuration decisions remain in force.

The Compose deployment will run the frontend in a container behind the existing Nginx gateway, which will be the sole host-facing entry for the UI and business APIs. The UI and APIs share an origin, with API access under `/api/v1/...`; repository clients will migrate from the old gateway address and paths rather than retain compatibility aliases. Reusing the existing gateway avoids a second proxy layer. The IdP remains a separate service and non-Web interfaces such as RabbitMQ are outside this boundary.

This supersedes the frontend and gateway topology described in [ADR-0016](0016-keycloak-device-flow-without-frontend.md), where the frontend is exposed separately and Next.js rewrites API requests to the gateway. It does not change the authentication flow or remove direct Bearer API access: clients use the new gateway API paths.

The UI deployment remains optional for standalone market-data consumers. The entry defaults to loopback and supports separately selected HTTP and HTTPS modes; HTTPS certificates are supplied and maintained by the deployer. Local and deployment configurations use Rauthy through the generic OIDC contract, with environment-specific issuer URLs. Frontend public configuration remains fixed at image build time, favoring the existing build model over a reusable runtime-configured image. Detailed deployment requirements and acceptance criteria are in [the deployment design](../design/compose-unified-web-entry.md).
