#!/usr/bin/env bash
# Export the three stores into a portable bundle (M8 Task 2).
#
# The corpus is ~2.4 GB of Docker volumes: PostgreSQL 442 MB, Neo4j 1.44 GB,
# Qdrant 483 MB. Shipping the volumes verbatim would work and would be much larger
# and much less checkable, so each store is exported through its own tool and the
# bundle carries a manifest of counts.
#
# The manifest is the point. scripts/import_corpus.sh compares against it and
# fails if the restored counts differ, so "the corpus arrived intact" is a check
# and not an assumption. A vector store that silently restored 9,000 of 10,000
# points would still answer every question -- slightly worse, invisibly.
#
#     bash scripts/export_corpus.sh [OUTPUT_DIR]

set -euo pipefail

OUT="${1:-corpus_bundle}"
ENV_FILE="${ENV_FILE:-.env}"

PG_CONTAINER="${PG_CONTAINER:-enterprise_ai_postgres_deploy}"
NEO_CONTAINER="${NEO_CONTAINER:-enterprise_ai_neo4j_deploy}"
NEO_VOLUME="${NEO_VOLUME:-enterprise_ai_enterprise_neo4j_data}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

QDRANT_URL="http://${QDRANT_HOST:-localhost}:${QDRANT_HTTP_PORT:-6333}"
COLLECTION="${QDRANT_COLLECTION:?QDRANT_COLLECTION is not set}"

mkdir -p "$OUT"

# ------------------------------------------------------------------ PostgreSQL
log "PostgreSQL"
docker exec "$PG_CONTAINER" pg_dump \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=9 \
    > "$OUT/postgres.dump"

PG_TABLES=$(docker exec "$PG_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
PG_ROWS=$(docker exec "$PG_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT coalesce(sum(n_live_tup),0) FROM pg_stat_user_tables;")

echo "  tables=$PG_TABLES  approx_rows=$PG_ROWS  bytes=$(wc -c < "$OUT/postgres.dump")"

# ------------------------------------------------------------------ Neo4j
# neo4j-admin cannot dump a running database, so the container stops for the
# duration. This is an offline export by necessity, not by choice.
log "Neo4j (stopping the container for the dump)"
NEO_NODES=$(docker exec "$NEO_CONTAINER" cypher-shell \
    -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" --format plain \
    "MATCH (n) RETURN count(n);" | tail -1 | tr -d '\r')
NEO_RELS=$(docker exec "$NEO_CONTAINER" cypher-shell \
    -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" --format plain \
    "MATCH ()-[r]->() RETURN count(r);" | tail -1 | tr -d '\r')

docker stop "$NEO_CONTAINER" >/dev/null
trap 'docker start "$NEO_CONTAINER" >/dev/null || true' EXIT

docker run --rm \
    -v "$NEO_VOLUME:/data" \
    -v "$(pwd)/$OUT:/dumps" \
    neo4j:5-community \
    neo4j-admin database dump neo4j --to-path=/dumps --overwrite-destination=true

docker start "$NEO_CONTAINER" >/dev/null
trap - EXIT

echo "  nodes=$NEO_NODES  relationships=$NEO_RELS  bytes=$(wc -c < "$OUT/neo4j.dump")"

# ------------------------------------------------------------------ Qdrant
log "Qdrant"
SNAPSHOT=$(curl -fsS -X POST "$QDRANT_URL/collections/$COLLECTION/snapshots" \
    -H "api-key: $QDRANT_API_KEY" | python -c "import sys,json;print(json.load(sys.stdin)['result']['name'])")

curl -fsS "$QDRANT_URL/collections/$COLLECTION/snapshots/$SNAPSHOT" \
    -H "api-key: $QDRANT_API_KEY" -o "$OUT/qdrant.snapshot"

# Clean up the server-side copy; the bundle is the artefact.
curl -fsS -X DELETE "$QDRANT_URL/collections/$COLLECTION/snapshots/$SNAPSHOT" \
    -H "api-key: $QDRANT_API_KEY" >/dev/null

QD_POINTS=$(curl -fsS "$QDRANT_URL/collections/$COLLECTION" -H "api-key: $QDRANT_API_KEY" \
    | python -c "import sys,json;print(json.load(sys.stdin)['result']['points_count'])")

echo "  collection=$COLLECTION  points=$QD_POINTS  bytes=$(wc -c < "$OUT/qdrant.snapshot")"

# ------------------------------------------------------------------ Manifest
log "Manifest"
python - "$OUT" "$COLLECTION" "$PG_TABLES" "$PG_ROWS" "$NEO_NODES" "$NEO_RELS" "$QD_POINTS" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

out, collection, tables, rows, nodes, rels, points = sys.argv[1:8]
directory = Path(out)

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

manifest = {
    "exported_at": datetime.now(timezone.utc).isoformat(),
    "postgres": {"file": "postgres.dump", "tables": int(tables),
                 "approx_rows": int(rows)},
    "neo4j": {"file": "neo4j.dump", "nodes": int(nodes),
              "relationships": int(rels)},
    "qdrant": {"file": "qdrant.snapshot", "collection": collection,
               "points": int(points)},
}

for section in ("postgres", "neo4j", "qdrant"):
    path = directory / manifest[section]["file"]
    manifest[section]["bytes"] = path.stat().st_size
    manifest[section]["sha256"] = digest(path)

(directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(json.dumps(manifest, indent=2))
PY

log "Bundle written to $OUT"
du -sh "$OUT"
