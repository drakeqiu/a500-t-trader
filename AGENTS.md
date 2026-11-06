# Agent instructions

## Agent skills

### Issue tracker

Issues and specs live in this repo's GitHub Issues; use the `gh` CLI.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default labels: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context layout: root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

## Local builds and network sources

- Build container images locally with Podman, including Compose builds.
- For local Python dependency installation and builds, use the Aliyun PyPI mirror (`https://mirrors.aliyun.com/pypi/simple/`); the repository's `uv.toml` already configures uv.
- For local TypeScript/JavaScript dependency installation and builds, use the Aliyun npm mirror (`https://registry.npmmirror.com/`), including when using Bun.
- For local container builds, configure Podman to pull base images through the corresponding DaoCloud registry mirror (for example, `docker.m.daocloud.io` for Docker Hub and `ghcr.m.daocloud.io` for GHCR). Keep image names and GitHub Actions configuration portable; configure mirrors locally rather than hard-coding them into shared Dockerfiles or CI.
- Access GitHub directly, without a GitHub proxy or mirror. GitHub Actions should use upstream dependency and container registries, not the local mirror settings.

## Frontend

- Keep frontend dependencies managed independently in `frontend/` with Bun; keep the existing Python backend managed with uv.
- Treat the existing REST API as the source of truth. The frontend must use it for backend interactions and must not duplicate backend business logic or introduce a second backend.
- Prefer Matt Pocock Skills for requirements clarification, solution design, and code-quality checks. Follow Next.js App Router and TypeScript best practices.
- Use Impeccable Skills first for frontend UI/UX design and refinement; follow established design conventions and ensure consistency, responsive behavior, and accessibility.
- Prefer existing code and patterns. Avoid unnecessary dependencies, abstractions, and large-scale refactors.

- Frontend changes must include unit tests and end-to-end tests for the affected behavior; run both suites before merging.
