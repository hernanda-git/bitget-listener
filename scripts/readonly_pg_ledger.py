"""Read-only: print the REAL ledger from Postgres (fatty_config).

Reads PG connection info from the systemd unit file (never printed).
No writes, no DDL. SELECT-only.
"""
import os
import re
import sys

UNIT = "/etc/systemd/system/bitget-listener.service"

# pull env from the unit file so we never inline secrets
env = {}
with open(UNIT) as fh:
    for line in fh:
        m = re.match(r"\s*Environment=(\w+)=(\S+)", line)
        if m:
            env[m.group(1)] = m.group(2)

os.environ.setdefault("PGHOST", env.get("PGHOST", "localhost"))
os.environ.setdefault("PGPORT", env.get("PGPORT", "5432"))
os.environ.setdefault("PGDATABASE", env.get("PGDATABASE", "fatty_config"))
os.environ.setdefault("PGUSER", env.get("PGUSER", "app"))
os.environ.setdefault("PGPASSWORD", env.get("PGPASSWORD", ""))

import psycopg  # noqa: E402

try:
    with psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"], connect_timeout=10,
    ) as conn:
        with conn.cursor() as cur:
            print("=== tables in fatty_config ===")
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public' ORDER BY table_name;")
            tables = [r[0] for r in cur.fetchall()]
            print(tables)

            candidates = [t for t in tables if "trade" in t.lower()
                          or "ledger" in t.lower() or "equity" in t.lower()
                          or "outcome" in t.lower()]
            print(f"\n=== candidate trade/ledger tables: {candidates} ===")
            for t in candidates:
                cur.execute(f"SELECT COUNT(*) FROM {t};")
                n = cur.fetchone()[0]
                print(f"\n--- {t}: {n} rows ---")
                if n > 0:
                    cur.execute(f"SELECT * FROM {t} ORDER BY 1 DESC LIMIT 10;")
                    cols = [d.name for d in cur.description]
                    print("  cols:", cols)
                    for row in cur.fetchall():
                        print("  ", dict(zip(cols, row)))
except Exception as e:
    print(f"PG ERROR: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)
