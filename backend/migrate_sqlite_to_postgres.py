"""
Migrate data from SQLite → PostgreSQL.
Run inside the backend container after postgres is healthy:
  docker exec img-flow-backend-1 python backend/migrate_sqlite_to_postgres.py
"""
import os
import sqlite3
import sys

from sqlalchemy import create_engine, text

SQLITE_PATH = os.getenv("SQLITE_PATH", "/app/backend/local_storage/database.db")
PG_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://imgflow:imgflow@db:5432/imgflow",
)

if not os.path.exists(SQLITE_PATH):
    print(f"[ERROR] SQLite file not found: {SQLITE_PATH}")
    sys.exit(1)

print(f"[migrate] Source : {SQLITE_PATH}")
print(f"[migrate] Target : {PG_URL}")

sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_conn.row_factory = sqlite3.Row

pg_engine = create_engine(PG_URL)

# Tables in insert-safe order (parents before children)
TABLES = [
    "users",
    "global_contexts",
    "courses",
    "media_sessions",
    "visual_plans",
    "audio_segments",
    "generated_images",
    "segment_visual_plans",
    "download_logs",
]

with pg_engine.connect() as pg:
    # Disable FK checks during load so we don't have to worry about ordering edge cases
    pg.execute(text("SET session_replication_role = replica;"))

    for table in TABLES:
        # Check table exists in sqlite
        cur = sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if not cur.fetchone():
            print(f"  [skip]  {table} — not in SQLite")
            continue

        rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"  [skip]  {table} — empty")
            continue

        cols = [d[0] for d in sqlite_conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
        placeholders = ", ".join([f":{c}" for c in cols])
        col_names = ", ".join(cols)

        # Clear existing rows in postgres table first
        pg.execute(text(f"DELETE FROM {table}"))

        insert_sql = text(f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})")
        # Detect boolean columns in postgres so we can cast SQLite 0/1 integers
        bool_cols = set()
        col_info = pg.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND data_type = 'boolean'"
        ), {"t": table}).fetchall()
        bool_cols = {r[0] for r in col_info}

        batch = []
        for row in rows:
            r = dict(row)
            for col in bool_cols:
                if col in r and r[col] is not None:
                    r[col] = bool(r[col])
            batch.append(r)

        pg.execute(insert_sql, batch)
        print(f"  [ok]    {table} — {len(batch)} row(s)")

    pg.execute(text("SET session_replication_role = DEFAULT;"))
    pg.commit()

sqlite_conn.close()
print("\n[migrate] Done ✓ — all data copied to PostgreSQL.")
