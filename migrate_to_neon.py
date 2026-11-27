"""One-time migration script: local SQLite (data/sr_database.db) -> Neon PostgreSQL (DATABASE_URL)."""
import os
import sys
import sqlite3
import psycopg2
import psycopg2.extras

from app.database import (
    DB_PATH,
    _PUMP_VIEWS_SQL,
    _PgConnection,
    get_database_url,
    get_resource_path,
)

# Parent-first insertion order so foreign keys (e.g., app_sessions -> app_users, app_custom_values -> app_custom_fields) hold.
TABLE_ORDER = [
    "tblwells",
    "tblwelldata",
    "tblrodgrades",
    "tblpumpdata",
    "tblTBGdata",
    "tblTBGdatanew",
    "tblrodstringdata1",
    "tblrodstringdata2",
    "tblrodsdatanew",
    "tblrodstracking1",
    "tblrodstracking2",
    "tblwoverdata",
    "tblwearcommentsdata1",
    "tblwearcommentsdata2",
    "tblwearcommentsdatanew",
    "tbldatanew",
    "app_users",
    "app_sessions",
    "app_audit_log",
    "app_dropdown_options",
    "app_custom_fields",
    "app_custom_values",
    "app_builtin_fields",
    "app_settings",
    "app_pump_intake_items",
]

EXTRA_DDL = """
ALTER TABLE "app_custom_fields" ADD COLUMN IF NOT EXISTS "section_key" TEXT NOT NULL DEFAULT '';
ALTER TABLE "app_custom_fields" ADD COLUMN IF NOT EXISTS "placeholder" TEXT NOT NULL DEFAULT '';
ALTER TABLE "app_custom_fields" ADD COLUMN IF NOT EXISTS "default_value" TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS "app_builtin_fields" (
    "id"            INTEGER PRIMARY KEY AUTOINCREMENT,
    "target_page"   TEXT NOT NULL,
    "section_key"   TEXT NOT NULL,
    "input_name"    TEXT NOT NULL,
    "label"         TEXT NOT NULL,
    "input_type"    TEXT NOT NULL DEFAULT 'text',
    "sort_order"    INTEGER NOT NULL DEFAULT 10,
    "active"        INTEGER NOT NULL DEFAULT 1,
    "placeholder"   TEXT NOT NULL DEFAULT '',
    "default_value" TEXT NOT NULL DEFAULT '',
    UNIQUE("target_page", "input_name")
);
"""


def coerce_pg_value(val, data_type: str, is_nullable: bool):
    """Clean legacy SQLite values (e.g. '$0.00' or '' in numeric columns) for strict PostgreSQL types."""
    dt = (data_type or "").lower()
    is_int = "int" in dt
    is_float = any(k in dt for k in ("double", "real", "numeric", "decimal", "float"))

    if val is None:
        return None if is_nullable else (0 if (is_int or is_float) else "")

    if isinstance(val, str):
        s = val.strip()
        if s == "":
            if is_int or is_float:
                return None
            return "" if not is_nullable else val
        if is_int:
            cleaned = s.replace("$", "").replace(",", "").strip()
            try:
                return int(float(cleaned))
            except ValueError:
                return None
        if is_float:
            cleaned = s.replace("$", "").replace(",", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return None
        return val

    if isinstance(val, float) and is_int:
        return int(val)
    return val


def migrate(sqlite_path: str, pg_dsn: str) -> None:
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    if not pg_dsn:
        raise ValueError("DATABASE_URL is required.")

    src = sqlite3.connect(f"file:{os.path.abspath(sqlite_path)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    raw_pg = psycopg2.connect(pg_dsn)
    pg = _PgConnection(raw_pg, pool=None)

    try:
        schema_file = get_resource_path(os.path.join("app", "schema.sql"))
        with open(schema_file, "r", encoding="utf-8") as sf:
            schema_sql = sf.read()

        print("1. Creating tables, indexes, and views in Neon PostgreSQL...")
        pg.executescript(schema_sql)
        pg.executescript(EXTRA_DDL)
        pg.executescript(_PUMP_VIEWS_SQL)
        pg.commit()

        sqlite_tables = [
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        ordered_tables = [t for t in TABLE_ORDER if t in sqlite_tables] + [
            t for t in sqlite_tables if t not in TABLE_ORDER
        ]

        print("2. Clearing existing rows in target tables (idempotent reset)...")
        truncate_list = ", ".join(f'"{t}"' for t in reversed(ordered_tables))
        raw_cur = raw_pg.cursor()
        raw_cur.execute(f"TRUNCATE TABLE {truncate_list} RESTART IDENTITY CASCADE")
        raw_pg.commit()

        print("3. Migrating table data from SQLite -> Neon PostgreSQL...")
        total_migrated = 0
        for tbl in ordered_tables:
            pg_cols_rows = pg.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ? "
                "ORDER BY ordinal_position",
                (tbl,),
            ).fetchall()
            pg_col_meta = {
                r["column_name"]: (r["data_type"], r["is_nullable"] == "YES")
                for r in pg_cols_rows
            }

            src_cur = src.execute(f'SELECT * FROM "{tbl}"')
            src_cols = [d[0] for d in src_cur.description]
            common_cols = [c for c in src_cols if c in pg_col_meta]
            if not common_cols:
                print(f"  [SKIP] {tbl}: no matching columns")
                continue

            rows = src_cur.fetchall()
            if rows:
                col_sql = ", ".join(f'"{c}"' for c in common_cols)
                insert_sql = f'INSERT INTO "{tbl}" ({col_sql}) VALUES %s'
                payload = [
                    tuple(
                        coerce_pg_value(r[c], pg_col_meta[c][0], pg_col_meta[c][1])
                        for c in common_cols
                    )
                    for r in rows
                ]
                psycopg2.extras.execute_values(raw_cur, insert_sql, payload, page_size=500)
            raw_pg.commit()

            # Sync identity sequence if table has an id or _rowid_pk column
            for pk_col in ("id", "_rowid_pk"):
                if pk_col in pg_col_meta:
                    raw_cur.execute(
                        "SELECT pg_get_serial_sequence(%s, %s)",
                        (f'"{tbl}"', pk_col),
                    )
                    seq_row = raw_cur.fetchone()
                    if seq_row and seq_row[0]:
                        seq_name = seq_row[0]
                        raw_cur.execute(
                            f'SELECT COALESCE(MAX("{pk_col}"), 1), MAX("{pk_col}") IS NOT NULL FROM "{tbl}"'
                        )
                        max_id, has_rows = raw_cur.fetchone()
                        raw_cur.execute("SELECT setval(%s, %s, %s)", (seq_name, max_id, has_rows))
                        raw_pg.commit()

            total_migrated += len(rows)
            print(f"  [OK] {tbl}: {len(rows)} rows")

        print(f"\n4. Verifying row counts across all {len(ordered_tables)} tables...")
        mismatches = []
        for tbl in ordered_tables:
            src_cnt = src.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            raw_cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
            pg_cnt = raw_cur.fetchone()[0]
            if src_cnt != pg_cnt:
                mismatches.append((tbl, src_cnt, pg_cnt))
        if mismatches:
            raise RuntimeError(f"Row count mismatch detected: {mismatches}")

        print(f"SUCCESS: Migrated and verified {total_migrated} rows across {len(ordered_tables)} tables in Neon PostgreSQL.")
    except Exception:
        raw_pg.rollback()
        raise
    finally:
        src.close()
        raw_pg.close()


if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else get_database_url()
    migrate(DB_PATH, dsn)
