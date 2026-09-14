#!/usr/bin/env bash
# Restore a bundle produced by scripts/export_corpus.sh into the deployed stack
# (M8 Task 2), then verify it against the manifest.
#
#     bash scripts/import_corpus.sh corpus_bundle/
#
# Verification is not optional here. Each store is checked against the counts
# recorded at export time and the script exits non-zero on any mismatch, because
# a partially restored corpus does not fail loudly -- it answers every question
# slightly worse, from evidence that is quietly missing.

set -euo pipefail

BUNDLE="${1:?usage: import_corpus.sh BUNDLE_DIR}"
COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env}"
ENV_FILE="${ENV_FILE:-deploy/.env}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }

FAILURES=0

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

read_manifest() {
    python -c "import json,sys;print(json.load(open('$BUNDLE/manifest.json'))$1)"
}

for file in postgres.dump neo4j.dump qdrant.snapshot manifest.json; do
    [[ -f "$BUNDLE/$file" ]] || { echo "missing $BUNDLE/$file" >&2; exit 1; }
done

log "Checksums"
python - "$BUNDLE" <<'PY'
import hashlib, json, sys
from pathlib import Path

directory = Path(sys.argv[1])
manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
bad = []

for section in ("postgres", "neo4j", "qdrant"):
    path = directory / manifest[section]["file"]
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    ok = h.hexdigest() == manifest[section]["sha256"]
    print(f"  {'ok  ' if ok else 'BAD '} {path.name}")
    if not ok:
        bad.append(path.name)

if bad:
    raise SystemExit(f"checksum mismatch: {bad} -- the bundle is corrupt, re-transfer it")
PY

$COMPOSE up -d postgres neo4j qdrant

log "Waiting for the stores"
for _ in $(seq 1 60); do
    if $COMPOSE exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then break; fi
    sleep 3
done

# ------------------------------------------------------------------ PostgreSQL
log "PostgreSQL restore"
$COMPOSE exec -T postgres pg_restore \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner \
    < "$BUNDLE/postgres.dump" || true   # pg_restore warns on absent objects

$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ANALYZE;" >/dev/null

PG_ROWS=$($COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT coalesce(sum(n_live_tup),0) FROM pg_stat_user_tables;" | tr -d '\r')
PG_EXPECTED=$(read_manifest "['postgres']['approx_rows']")

echo "  rows: $PG_ROWS (expected ~$PG_EXPECTED)"
python -c "
import sys
got, want = $PG_ROWS, $PG_EXPECTED
# n_live_tup is an estimate on both sides, so 2% is the tolerance, not zero.
sys.exit(0 if want == 0 or abs(got - want) <= max(1, 0.02 * want) else 1)
" || fail "PostgreSQL row count differs by more than 2%"

log "Read-only role (M6 Task 1)"
$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v readonly_user="$POSTGRES_READONLY_USER" \
    -v readonly_password="$POSTGRES_READONLY_PASSWORD" \
    < database/create_readonly_role.sql

# ------------------------------------------------------------------ Neo4j
log "Neo4j restore (offline load)"
$COMPOSE stop neo4j

NEO_VOLUME="$($COMPOSE config --format json 2>/dev/null \
    | python -c "import json,sys;print(json.load(sys.stdin)['volumes']['neo4j_data'].get('name','enterprise-ai_neo4j_data'))" \
    2>/dev/null || echo "enterprise-ai_neo4j_data")"

docker run --rm \
    -v "$NEO_VOLUME:/data" \
    -v "$(cd "$BUNDLE" && pwd):/dumps" \
    neo4j:5-community \
    neo4j-admin database load neo4j --from-path=/dumps --overwrite-destination=true

$COMPOSE start neo4j

for _ in $(seq 1 60); do
    if $COMPOSE exec -T neo4j cypher-shell -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
        "RETURN 1;" >/dev/null 2>&1; then break; fi
    sleep 3
done

NEO_NODES=$($COMPOSE exec -T neo4j cypher-shell -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" \
    --format plain "MATCH (n) RETURN count(n);" | tail -1 | tr -d '\r')
NEO_EXPECTED=$(read_manifest "['neo4j']['nodes']")

echo "  nodes: $NEO_NODES (expected $NEO_EXPECTED)"
[[ "$NEO_NODES" == "$NEO_EXPECTED" ]] || fail "Neo4j node count $NEO_NODES != $NEO_EXPECTED"

# ------------------------------------------------------------------ Qdrant
log "Qdrant restore"
COLLECTION=$(read_manifest "['qdrant']['collection']")

$COMPOSE exec -T qdrant sh -c 'mkdir -p /qdrant/snapshots/upload'
docker cp "$BUNDLE/qdrant.snapshot" \
    "$($COMPOSE ps -q qdrant):/qdrant/snapshots/upload/restore.snapshot"

$COMPOSE exec -T qdrant sh -c "
    curl -fsS -X PUT 'http://localhost:6333/collections/$COLLECTION/snapshots/recover' \
        -H 'api-key: $QDRANT_API_KEY' -H 'Content-Type: application/json' \
        -d '{\"location\":\"file:///qdrant/snapshots/upload/restore.snapshot\"}'
" >/dev/null

QD_POINTS=$($COMPOSE exec -T qdrant sh -c "
    curl -fsS 'http://localhost:6333/collections/$COLLECTION' -H 'api-key: $QDRANT_API_KEY'
" | python -c "import json,sys;print(json.load(sys.stdin)['result']['points_count'])")
QD_EXPECTED=$(read_manifest "['qdrant']['points']")

echo "  points: $QD_POINTS (expected $QD_EXPECTED)"
[[ "$QD_POINTS" == "$QD_EXPECTED" ]] || fail "Qdrant point count $QD_POINTS != $QD_EXPECTED"

# ------------------------------------------------------------------ Verdict
log "Result"

if [[ "$FAILURES" -gt 0 ]]; then
    echo "$FAILURES check(s) failed. The corpus is NOT verified -- do not serve from it."
    exit 1
fi

echo "All three stores restored and verified against the manifest."
