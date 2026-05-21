"""
One-time script: insert Charles and Amanda as admin accounts for both companies.
Run: python seed_admins.py
"""
import getpass
import os

import bcrypt
import psycopg2

ADMINS = [
    {"full_name": "Charles Rose", "email": "charles@dodcyberconsulting.com", "company": "dod"},
    {"full_name": "Amanda",       "email": "gypsyrose1@rocketmail.com",      "company": "dod"},
    {"full_name": "Charles Rose", "email": "rosecharlesrose@gmail.com",      "company": "rosesli"},
    {"full_name": "Amanda",       "email": "gypsyrose1@rocketmail.com",      "company": "rosesli"},
]

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
        print(f"Setting password for {admin['full_name']} <{admin['email']}> [{admin['company']}]")
        pw  = getpass.getpass("  Password: ")
        pw2 = getpass.getpass("  Confirm:  ")
        if pw != pw2:
            print("  Passwords do not match — skipping.\n")
            continue
        if len(pw) < 8:
            print("  Password must be at least 8 characters — skipping.\n")
            continue
        hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
        cur.execute(
            """INSERT INTO portal_users (email, password_hash, full_name, role, company, active)
               VALUES (%s, %s, %s, 'admin', %s, TRUE)
               ON CONFLICT (email) DO UPDATE
               SET password_hash = EXCLUDED.password_hash,
                   full_name     = EXCLUDED.full_name,
                   role          = 'admin',
                   active        = TRUE""",
            (admin["email"], hashed, admin["full_name"], admin["company"]),
        )
        print(f"  Saved.\n")

conn.commit()
conn.close()
print("Done.")
