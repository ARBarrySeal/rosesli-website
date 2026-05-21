"""Run once to create portal tables in botdb."""
import os
import psycopg2

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "botdb"),
    user=os.environ.get("DB_USER", "botuser"),
    password=os.environ.get("DB_PASS", ""),
)
sql_path = os.path.join(os.path.dirname(__file__), "migrations", "001_portal_tables.sql")
with open(sql_path) as f:
    sql = f.read()
with conn.cursor() as cur:
    cur.execute(sql)
conn.commit()
conn.close()
print("Migration complete.")
