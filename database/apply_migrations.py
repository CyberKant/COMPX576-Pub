"""Apply SQL migration files from database/migrations/ to Supabase, in filename order.

    python -m database.apply_migrations --yes              # all files
    python -m database.apply_migrations --yes 001 002      # only files whose name starts with these prefixes

Each file manages its own transaction (BEGIN/COMMIT). Files are written to be re-runnable.
"""
import argparse
from pathlib import Path

from database.database import get_postgres_connection

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def apply_migrations(prefixes: list[str]) -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if prefixes:
        files = [f for f in files if any(f.name.startswith(p) for p in prefixes)]
    if not files:
        print("[MIGRATE] No matching migration files.")
        return

    conn = get_postgres_connection()
    if conn is None:
        raise RuntimeError("Could not open a Postgres connection.")
    conn.autocommit = True  # the files carry their own BEGIN/COMMIT
    try:
        for path in files:
            print(f"[MIGRATE] Applying {path.name} ...")
            with conn.cursor() as cur:
                # Fail fast (and visibly) if another session holds a conflicting lock, instead of queueing behind it
                # and stalling every other query on the table
                cur.execute("SET lock_timeout = '15s'")
                cur.execute(path.read_text(encoding="utf-8"))
            print(f"[MIGRATE] {path.name} applied.")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply SQL migrations to Supabase.")
    parser.add_argument("prefixes", nargs="*", help="Only files whose name starts with one of these (e.g. 001 002).")
    parser.add_argument("--yes", action="store_true", help="Required: confirms you reviewed the migrations (001 truncates data).")
    args = parser.parse_args()
    if not args.yes:
        raise SystemExit("Refusing to run without --yes (migration 001 TRUNCATEs documents/document_chunks on first run).")
    apply_migrations(args.prefixes)
