"""Restore / Reload project backups (.db, .zip CSV bundle, .xlsx Full DB workbook)."""
import csv
import datetime
import hashlib
import io
import os
import sqlite3
import zipfile
from typing import Optional

import app.database as _db
from app.database import (
    _to_iso_date_str,
    _write_lock,
    create_backup,
    db_session,
    ensure_pump_views,
    get_connection,
    init_db,
    log_audit,
    normalize_all_dates,
)

# Parent-first order so deleting in reverse and inserting in forward order
# satisfies foreign keys (e.g., app_custom_values -> app_custom_fields).
RESTORABLE_TABLES = [
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
    "app_dropdown_options",
    "app_custom_fields",
    "app_custom_values",
    "app_builtin_fields",
    "app_pump_intake_items",
    "app_settings",
    "app_audit_log",
]

LEGACY_CORE_TABLES = {"tblwells", "tblpumpdata", "tblwoverdata", "tblTBGdata"}
EXCLUDED_SECURITY_TABLES = {"app_users", "app_sessions"}
IGNORED_VIEW_NAMES = {"vw_new_dh_pump_data", "vw_pump_teardown_data"}


def resolve_server_backup_path(filename: str) -> str:
    """Safely resolve a backup filename inside BACKUP_DIR (preventing path traversal)."""
    clean_name = os.path.basename((filename or "").strip())
    if not clean_name or clean_name != (filename or "").strip():
        raise ValueError("Invalid backup filename.")
    if not clean_name.lower().endswith(".db"):
        raise ValueError("Only .db server backups can be selected from /backups.")
    backup_root = os.path.abspath(_db.BACKUP_DIR)
    target = os.path.abspath(os.path.join(backup_root, clean_name))
    if os.path.dirname(target) != backup_root:
        raise ValueError("Invalid backup path.")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"Backup file not found: {clean_name}")
    return target


def _validate_sqlite_backup(db_path: str) -> list[str]:
    """Verify the file is a readable SQLite database containing SR core tables."""
    if not os.path.isfile(db_path) or os.path.getsize(db_path) < 100:
        raise ValueError("The selected file is empty or not a valid SQLite database.")
    with open(db_path, "rb") as f:
        header = f.read(16)
    if header != b"SQLite format 3\x00":
        raise ValueError("Invalid SQLite file header (expected a .db backup created by SR Database Server).")

    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        chk = conn.execute("PRAGMA quick_check(1);").fetchone()
        if not chk or str(chk[0]).lower() != "ok":
            raise ValueError(f"SQLite integrity check failed: {chk[0] if chk else 'unknown error'}")
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()

    lower_tables = {t.lower() for t in tables}
    missing = [t for t in LEGACY_CORE_TABLES if t.lower() not in lower_tables]
    if missing:
        raise ValueError(
            f"Not a compatible SR Database backup (missing core tables: {', '.join(sorted(missing))})."
        )
    return tables


def _sync_custom_layout_after_restore() -> None:
    """Reset custom-fields schema cache and sync data/builtin_layout.json with restored DB."""
    import app.custom_fields as cf

    cf._SCHEMA_READY = False
    cf._BUILTIN_LAYOUT_CACHE = None
    with db_session(write=True) as conn:
        cf._ensure_layout_schema(conn)
        cnt = conn.execute('SELECT COUNT(*) FROM "app_builtin_fields"').fetchone()[0]
        if cnt > 0:
            cf.save_builtin_layout_to_json(conn)


def _capture_active_session(raw_token: Optional[str]) -> Optional[dict]:
    if not raw_token or not os.path.exists(_db.DB_PATH):
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    try:
        with db_session() as conn:
            row = conn.execute(
                '''SELECT u."username", s."expires_at", s."ip_address"
                   FROM "app_sessions" s
                   JOIN "app_users" u ON u."id" = s."user_id"
                   WHERE s."token_hash"=? LIMIT 1''',
                (token_hash,),
            ).fetchone()
            if row:
                return {
                    "username": row["username"],
                    "token_hash": token_hash,
                    "expires_at": row["expires_at"],
                    "ip_address": row["ip_address"] or "",
                }
    except Exception:
        return None
    return None


def _preserve_session_if_valid(session_info: Optional[dict]) -> None:
    if not session_info:
        return
    from app.auth import is_setup_complete

    with db_session(write=True) as conn:
        if not is_setup_complete(conn):
            return
        user_row = conn.execute(
            'SELECT "id", "is_active", "password_hash" FROM "app_users" '
            'WHERE LOWER("username")=LOWER(?) LIMIT 1',
            (session_info["username"],),
        ).fetchone()
        if not user_row or not user_row["is_active"] or not (user_row["password_hash"] or "").strip():
            return
        conn.execute(
            'INSERT OR IGNORE INTO "app_sessions" ("token_hash","user_id","expires_at","ip_address") '
            'VALUES (?,?,?,?)',
            (
                session_info["token_hash"],
                user_row["id"],
                session_info["expires_at"],
                session_info["ip_address"],
            ),
        )


def restore_from_db_file(
    backup_db_path: str,
    source_label: str = "AdminUI",
    current_session_token: Optional[str] = None,
    display_name: str = "",
) -> dict:
    """Restore the entire SQLite database from a .db backup using SQLite online backup API."""
    _validate_sqlite_backup(backup_db_path)
    session_info = _capture_active_session(current_session_token)
    shown_name = (display_name or "").strip() or os.path.basename(backup_db_path)

    with _write_lock:
        if _db.is_postgres_enabled():
            safety_backup = os.path.basename(create_backup(tag="pre_restore"))
            src_conn = sqlite3.connect(f"file:{os.path.abspath(backup_db_path)}?mode=ro", uri=True)
            src_conn.row_factory = sqlite3.Row
            try:
                src_tables = [
                    r[0]
                    for r in src_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
                ordered_tables = [t for t in RESTORABLE_TABLES if t in src_tables] + [
                    t for t in src_tables if t not in RESTORABLE_TABLES and t not in EXCLUDED_SECURITY_TABLES
                ]
                with db_session(write=True) as dst_conn:
                    for t in reversed(ordered_tables):
                        try:
                            dst_conn.execute(f'DELETE FROM "{t}"')
                        except Exception:
                            pass
                    for t in ordered_tables:
                        cols_info = dst_conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                        dst_cols = [c[1] for c in cols_info]
                        src_cols = [d[0] for d in src_conn.execute(f'SELECT * FROM "{t}" LIMIT 0').description]
                        common = [c for c in src_cols if c in dst_cols]
                        if not common:
                            continue
                        col_sql = ", ".join(f'"{c}"' for c in common)
                        ph = ", ".join("?" for _ in common)
                        rows = src_conn.execute(f'SELECT {col_sql} FROM "{t}"').fetchall()
                        if rows:
                            dst_conn.executemany(
                                f'INSERT INTO "{t}" ({col_sql}) VALUES ({ph})',
                                [tuple(r[c] for c in common) for r in rows],
                            )
            finally:
                src_conn.close()
        else:
            safety_backup = os.path.basename(create_backup(tag="pre_restore")) if os.path.exists(_db.DB_PATH) else None
            src_conn = sqlite3.connect(f"file:{os.path.abspath(backup_db_path)}?mode=ro", uri=True)
            dst_conn = get_connection(_db.DB_PATH)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

    init_db(seed_if_empty=False)
    _sync_custom_layout_after_restore()
    _preserve_session_if_valid(session_info)

    results = []
    total_rows = 0
    with db_session(write=True) as conn:
        live_tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for t in live_tables:
            cnt = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if t != "app_sessions":
                total_rows += cnt
            results.append({"table": t, "status": "OK", "rows": cnt, "detail": "Restored from .db backup"})
        log_audit(
            conn,
            "RESTORE_DB",
            "ALL_TABLES",
            f"{shown_name} ({total_rows} rows, safety={safety_backup or 'none'})",
            source_label,
        )

    return {
        "method": "SQLite Hot Restore (.db)",
        "source_file": shown_name,
        "backup_file": safety_backup,
        "total_rows": total_rows,
        "tables": results,
    }


def _coerce_cell_value(val, col_info: dict):
    """Convert CSV/Excel cell values cleanly for SQLite column types and NOT NULL constraints."""
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%Y-%m-%d")

    is_blank = val is None or (isinstance(val, str) and val.strip() == "")
    col_type = (col_info["type"] or "").upper()
    is_int_col = bool(col_info["pk"]) or ("INT" in col_type)
    is_numeric_col = is_int_col or ("REAL" in col_type) or ("NUM" in col_type)

    if is_blank:
        if is_numeric_col:
            return None
        if col_info["notnull"]:
            return ""
        return None

    if isinstance(val, float) and is_int_col and val.is_integer():
        return int(val)

    if isinstance(val, str):
        s = val.strip()
        if is_int_col:
            try:
                return int(float(s))
            except ValueError:
                return s
        if "REAL" in col_type:
            try:
                return float(s)
            except ValueError:
                return s
        return val
    return val


def _apply_tabular_restore(
    table_payloads: dict[str, tuple[list[str], list[list]]],
    skipped_items: list[dict],
    method_label: str,
    audit_action: str,
    source_name: str,
    source_label: str,
) -> dict:
    """Atomically reload parsed table data into SQLite while preserving user auth tables."""
    # Ensure at least one core operational table is present before mutating DB
    has_core = any(t in table_payloads for t in LEGACY_CORE_TABLES)
    if not has_core:
        raise ValueError(
            "No recognized SR Database operational tables (e.g. tblwells, tblpumpdata, tblwoverdata, tblTBGdata) were found in this file."
        )

    init_db(seed_if_empty=False)
    import app.custom_fields as cf

    with db_session(write=True) as conn:
        cf._ensure_layout_schema(conn)

    with _write_lock:
        safety_backup = os.path.basename(create_backup(tag="pre_restore")) if os.path.exists(_db.DB_PATH) else None

        results = list(skipped_items)
        total_rows = 0
        restored_builtin = False

        with db_session(write=True) as conn:
            ordered_targets = [t for t in RESTORABLE_TABLES if t in table_payloads]
            validated = {}
            for t in ordered_targets:
                cols, rows = table_payloads[t]
                pragma_rows = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                col_meta = {
                    r[1].lower(): {
                        "name": r[1],
                        "type": r[2] or "",
                        "notnull": bool(r[3]),
                        "pk": bool(r[5]),
                    }
                    for r in pragma_rows
                }
                matched = [(i, col_meta[c.strip().lower()]) for i, c in enumerate(cols) if c and c.strip().lower() in col_meta]
                if not matched:
                    results.append({"table": t, "status": "WARN", "rows": 0, "detail": "No matching columns"})
                    continue
                validated[t] = (matched, rows)

            # Delete children before parents
            for t in reversed(ordered_targets):
                if t in validated:
                    conn.execute(f'DELETE FROM "{t}"')

            # Insert parents before children
            for t in ordered_targets:
                if t not in validated:
                    continue
                matched, rows = validated[t]
                col_sql = ", ".join(f'"{info["name"]}"' for _, info in matched)
                q_sql = ", ".join("?" for _ in matched)
                insert_sql = f'INSERT INTO "{t}" ({col_sql}) VALUES ({q_sql})'
                payload = [
                    tuple(_coerce_cell_value(r[i] if i < len(r) else None, info) for i, info in matched)
                    for r in rows
                ]
                if payload:
                    conn.executemany(insert_sql, payload)
                total_rows += len(payload)
                if t == "app_builtin_fields":
                    restored_builtin = True
                results.append({"table": t, "status": "OK", "rows": len(payload), "detail": "Reloaded"})

            normalize_all_dates(conn)
            ensure_pump_views(conn)
            log_audit(
                conn,
                audit_action,
                "ALL_TABLES",
                f"{source_name} ({total_rows} rows, safety={safety_backup or 'none'})",
                source_label,
            )

    if restored_builtin:
        _sync_custom_layout_after_restore()

    return {
        "method": method_label,
        "source_file": source_name,
        "backup_file": safety_backup,
        "total_rows": total_rows,
        "tables": results,
    }


def restore_from_csv_zip(zip_path: str, source_label: str = "AdminUI", display_name: str = "") -> dict:
    """Reload table data from a CSV Bundle (.zip) exported by /api/backup/csv-zip."""
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("Uploaded file is not a valid .zip archive.")

    canonical_map = {t.lower(): t for t in RESTORABLE_TABLES}
    table_payloads: dict[str, tuple[list[str], list[list]]] = {}
    skipped: list[dict] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            base = os.path.basename(member)
            stem, ext = os.path.splitext(base)
            if ext.lower() != ".csv" or not stem:
                continue
            stem_lower = stem.lower()
            if stem_lower in IGNORED_VIEW_NAMES:
                skipped.append({"table": stem, "status": "SKIP", "rows": 0, "detail": "View (auto-generated)"})
                continue
            if stem_lower in EXCLUDED_SECURITY_TABLES:
                skipped.append({"table": stem, "status": "SKIP", "rows": 0, "detail": "Auth table preserved for security"})
                continue
            target_table = canonical_map.get(stem_lower)
            if not target_table:
                skipped.append({"table": stem, "status": "SKIP", "rows": 0, "detail": "Unknown table"})
                continue

            raw_text = zf.read(member).decode("utf-8-sig", errors="replace")
            parsed = list(csv.reader(io.StringIO(raw_text)))
            if not parsed:
                skipped.append({"table": target_table, "status": "SKIP", "rows": 0, "detail": "Empty CSV"})
                continue
            cols = parsed[0]
            rows = [r for r in parsed[1:] if any(str(cell).strip() != "" for cell in r)]
            table_payloads[target_table] = (cols, rows)

    return _apply_tabular_restore(
        table_payloads=table_payloads,
        skipped_items=skipped,
        method_label="CSV Bundle Reload (.zip)",
        audit_action="RESTORE_CSV_ZIP",
        source_name=display_name or os.path.basename(zip_path),
        source_label=source_label,
    )


def restore_from_excel(xlsx_path: str, source_label: str = "AdminUI", display_name: str = "") -> dict:
    """Reload table data from a Full DB Excel (.xlsx) exported by /api/backup/excel."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"Could not read Excel workbook (.xlsx): {e}") from e

    canonical_map = {t.lower(): t for t in RESTORABLE_TABLES}
    table_payloads: dict[str, tuple[list[str], list[list]]] = {}
    skipped: list[dict] = []

    try:
        for sheet_name in wb.sheetnames:
            s_lower = sheet_name.strip().lower()
            if s_lower in IGNORED_VIEW_NAMES:
                skipped.append({"table": sheet_name, "status": "SKIP", "rows": 0, "detail": "View (auto-generated)"})
                continue
            if s_lower in EXCLUDED_SECURITY_TABLES:
                skipped.append({"table": sheet_name, "status": "SKIP", "rows": 0, "detail": "Auth table preserved for security"})
                continue
            target_table = canonical_map.get(s_lower)
            if not target_table:
                skipped.append({"table": sheet_name, "status": "SKIP", "rows": 0, "detail": "Unknown sheet"})
                continue

            ws = wb[sheet_name]
            row_iter = ws.iter_rows(values_only=True)
            header_row = next(row_iter, None)
            if not header_row:
                skipped.append({"table": target_table, "status": "SKIP", "rows": 0, "detail": "Empty sheet"})
                continue
            cols = [str(c).strip() if c is not None else "" for c in header_row]
            rows = []
            for r in row_iter:
                r_list = list(r)
                if any(v is not None and str(v).strip() != "" for v in r_list):
                    rows.append(r_list)
            table_payloads[target_table] = (cols, rows)
    finally:
        wb.close()

    return _apply_tabular_restore(
        table_payloads=table_payloads,
        skipped_items=skipped,
        method_label="Full DB Excel Reload (.xlsx)",
        audit_action="RESTORE_EXCEL",
        source_name=display_name or os.path.basename(xlsx_path),
        source_label=source_label,
    )


def restore_from_uploaded_backup(
    file_path: str,
    original_filename: str,
    source_label: str = "AdminUI",
    current_session_token: Optional[str] = None,
) -> dict:
    """Dispatch restore/reload based on uploaded backup file extension (.db, .zip, .xlsx)."""
    ext = os.path.splitext(original_filename or file_path)[1].lower()
    if ext in (".db", ".sqlite", ".sqlite3"):
        return restore_from_db_file(
            file_path,
            source_label=source_label,
            current_session_token=current_session_token,
            display_name=original_filename,
        )
    if ext == ".zip":
        return restore_from_csv_zip(
            file_path,
            source_label=source_label,
            display_name=original_filename,
        )
    if ext == ".xlsx":
        return restore_from_excel(
            file_path,
            source_label=source_label,
            display_name=original_filename,
        )
    raise ValueError("Unsupported backup file type. Please upload a .db, .zip (CSV bundle), or .xlsx (Full DB Excel) file.")
