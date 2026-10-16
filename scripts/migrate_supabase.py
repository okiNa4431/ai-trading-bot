"""Migration-only owner connection. API secret keys cannot perform arbitrary DDL."""

import hashlib
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv


def main() -> int:
    load_dotenv(override=False)
    uri = os.environ.get("SUPABASE_DB_URL")
    if not uri:
        print(
            "Migration not applied: SUPABASE_DB_URL (owner PostgreSQL connection) is absent. "
            "Data API keys cannot apply DDL. Run supabase/bootstrap.sql once in SQL Editor."
        )
        return 2
    try:
        with psycopg.connect(uri, connect_timeout=20, sslmode="require") as connection:
            with connection.transaction():
                connection.execute("select pg_advisory_xact_lock(490016000)")
                connection.execute("create schema if not exists bot_private")
                connection.execute(
                    "revoke all on schema bot_private from public,anon,authenticated"
                )
                connection.execute(
                    "create table if not exists bot_private.migrations "
                    "(name text primary key, sha256 text not null)"
                )
                for path in sorted(Path("supabase/migrations").glob("*.sql")):
                    content = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    old = connection.execute(
                        "select sha256 from bot_private.migrations where name=%s", (path.name,)
                    ).fetchone()
                    if old:
                        if old[0] != digest:
                            raise ValueError("applied migration checksum changed")
                        continue
                    connection.execute(content, prepare=False)
                    connection.execute(
                        "insert into bot_private.migrations values(%s,%s)", (path.name, digest)
                    )
        print("Migration applied successfully; credentials and connection URI are not displayed.")
        return 0
    except Exception:
        print("Migration failed safely; inspect owner connection and SQL. No partial migration.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
