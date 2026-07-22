# AskAlfred agent guide

These instructions apply to the whole repository.

## Start here

- Read `README.md` for setup and the package overview.
- Read `ARCHITECTURE.md` before changing boundaries, cross-package flows, ingestion,
  retrieval, access control, or degradation behaviour.
- Use `project-map.yaml` as a generated index, not as a substitute for inspecting
  the code. Search it by path or symbol to find likely implementation and test files.

## Architecture

- Keep query orchestration in `query_core/`, intent behaviour in `query_handlers/`,
  and retrieval mechanics in `search_core/`.
- Keep building identity rules in `building/` and FRA rules in `fra/`.
- Depend on contracts in `interfaces/` at infrastructure boundaries.
- Preserve access-control filtering through every structured and semantic retrieval
  path. Treat changes in `auth/`, `security/`, and credential handling as sensitive.
- Preserve explicit degraded outcomes and operator telemetry; do not turn dependency
  failures into silent empty results.

## Change workflow

- Prefer a focused test near the affected module and run the smallest relevant pytest
  selection before the full suite.
- Run `poetry run ruff check .` and `poetry run pytest tests -q` for broad changes.
- Do not commit `.env`, credentials, local data, logs, model artefacts, caches, or
  generated metrics.
- Keep unrelated user changes intact.

## Generated project map

- Do not edit `project-map.yaml` directly.
- Put durable human metadata in `project-map-overrides.json`.
- Run `poetry run python tools/generate_project_map.py` after adding, removing,
  renaming, or changing the public symbols of a file.
- Add `--check` to verify freshness. CI enforces this.
- The generator includes tracked and non-ignored untracked files. Empty directories
  are not represented because Git does not track them.
