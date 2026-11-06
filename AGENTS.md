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

## Frontend

- Keep frontend dependencies managed independently in `frontend/` with Bun; keep the existing Python backend managed with uv.
- Treat the existing REST API as the source of truth. The frontend must use it for backend interactions and must not duplicate backend business logic or introduce a second backend.
- Prefer Matt Pocock Skills for requirements clarification, solution design, and code-quality checks. Follow Next.js App Router and TypeScript best practices.
- Use Impeccable Skills first for frontend UI/UX design and refinement; follow established design conventions and ensure consistency, responsive behavior, and accessibility.
- Prefer existing code and patterns. Avoid unnecessary dependencies, abstractions, and large-scale refactors.

- Frontend changes must include unit tests and end-to-end tests for the affected behavior; run both suites before merging.
