"""
One-time script: insert Charles and Amanda as admin accounts for both companies.
Run: python seed_admins.py
Override password: set ADMIN_PASSWORD env var before running.
"""
import os

import bcrypt
import psycopg2

ADMINS = [
    {"full_name": "Charles Rose", "email": "charles@dodcyberconsulting.com", "company": "dod"},
    {"full_name": "Amanda",       "email": "gypsyrose1@rocketmail.com",      "company": "dod"},
    {"full_name": "Charles Rose", "email": "rosecharlesrose@gmail.com",      "company": "rosesli"},
    {"full_name": "Amanda",       "email": "gypsyrose1@rocketmail.com",      "company": "rosesli"},
]

pw = os.environ.get("ADMIN_PASSWORD", "AmyChuck1!")
hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ.get("DB_NAME", "botdb"),
    user=os.environ.get("DB_USER", "botuser"),
    password=os.environ.get("DB_PASS", ""),
)

print("Seeding admin accounts.\n")

with conn.cursor() as cur:
    for admin in ADMINS:
        print(f"  {admin['full_name']} <{admin['email']}> [{admin['company']}]")
        cur.execute(
            """INSERT INTO portal_users (email, password_hash, full_name, role, company, active)
               VALUES (%s, %s, %s, 'admin', %s, TRUE)
               ON CONFLICT (email, company) DO UPDATE
               SET password_hash = EXCLUDED.password_hash,
                   full_name     = EXCLUDED.full_name,
                   role          = 'admin',
                   active        = TRUE""",
            (admin["email"], hashed, admin["full_name"], admin["company"]),
        )

conn.commit()
conn.close()
print("Done.")
