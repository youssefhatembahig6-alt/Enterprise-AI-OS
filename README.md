# Enterprise AI OS

Application repository for the Enterprise AI OS graduation project. This branch delivers **milestone M0**: a runnable local foundation with tenant isolation, login and a tested reference authorization policy. There are no LLM, RAG, SQL-agent or connector features yet.

| Path | Contents |
| --- | --- |
| `backend/` | FastAPI app (`app/api`, `services`, `repositories`, `security`, `tasks`, `db`), Alembic migrations, fixtures, tests |
| `apps/web/` | React + Vite + Tailwind sign-in and identity screen |
| `packages/ui/` | Shared React components and theme tokens (reused by the later Electron shell) |
| `scripts/` | Portable Python helpers: `dev.py` and `smoke_stack.py` |
| `infra/postgres/` | First-run role bootstrap for the PostgreSQL container |
| `docs/` | ADRs, contracts, runbook, OpenAPI schema and traceability |

Quick start: [docs/runbook.md](docs/runbook.md). Decisions: [docs/adr/](docs/adr/README.md). API schema: [docs/openapi.json](docs/openapi.json).

## Pull request reviews

Implement each milestone on its own branch, run the assignment checks and open a pull request targeting `main`. Include test results and unresolved checklist items. Share the PR link in the planning chat for review before merging.
