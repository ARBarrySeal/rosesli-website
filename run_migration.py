"""Apply a migration SQL file to the portal database.

Usage:
    python run_migration.py                       # applies the latest migrations/*.sql
    python run_migration.py 005_scheduler.sql     # applies a specific migration

Connection is read from DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS env vars.
Migrations are written to be idempotent, so re-running is safe.
"""
import glob
import os
import sys

import psycopg2

MIG_DIR = os.path.join(os.path.dirname(__file__), "migrations")

if len(sys.argv) > 1:
    sql_path = os.path.join(MIG_DIR, sys.argv[1])
else:
    candidates = sorted(glob.glob(os.path.join(MIG_DIR, "*.sql")))
    if not candidates:
        sys.exit("No migration files found.")
    sql_path = candidates[-1]

if not os.path.exists(sql_path):
    sys.exit(f"Migration not found: {sql_path}")

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "botdb"),
    user=os.environ.get("DB_USER", "botuser"),
    password=os.environ.get("DB_PASS", ""),
)
with open(sql_path) as f:
    sql = f.read()
with conn.cursor() as cur:
    cur.execute(sql)
conn.commit()
conn.close()
print(f"Migration complete: {os.path.basename(sql_path)}")
