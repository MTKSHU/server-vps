#!/usr/bin/env sh
# Export the backend OpenAPI spec and regenerate frontend TypeScript types.
#
# Usage:
#   ./scripts/export-openapi.sh
#
# Prerequisites:
#   - The backend Docker container (deploy-backend-1) must be running.
#   - Run from the repository root.

set -e

CONTAINER="${BACKEND_CONTAINER:-deploy-backend-1}"
OUT="frontend/openapi.json"

echo "Exporting OpenAPI spec from container: $CONTAINER"
docker exec "$CONTAINER" python3 -c "
import json
from app.main import app
print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
" > "$OUT"
echo "  -> $OUT ($(wc -l < "$OUT") lines)"

echo "Regenerating frontend/src/api/schema.d.ts"
cd frontend
npm run gen-api
echo "  -> src/api/schema.d.ts"

echo ""
echo "Done. Commit both files to keep the API contract in sync:"
echo "  git add frontend/openapi.json frontend/src/api/schema.d.ts"
