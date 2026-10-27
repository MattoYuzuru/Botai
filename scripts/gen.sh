#!/usr/bin/env bash
# Regenerates everything derived from contracts and the backend API. CI fails if output drifts.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cp "$root"/contracts/*.schema.json "$root/backend/src/botai/contracts/schemas/"
(cd "$root/backend" && uv run --frozen python -c "import json; from botai.main import create_app; print(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True))" > openapi.json)
pnpm --filter @botai/web gen:api >/dev/null
pnpm --filter @botai/web gen:blocks >/dev/null
pnpm exec biome format --write apps/web/src/api >/dev/null 2>&1 || true
echo "generated: schemas copy, backend/openapi.json, apps/web/src/api/*.gen.ts"
