import os
import re
import sys
import io
import csv
import glob
import sqlite3
import zipfile
import threading
import contextvars
from datetime import datetime
from contextlib import contextmanager
from urllib.parse import urlparse

try:
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras
except ImportError:
    psycopg2 = None


def get_base_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_resource_path(relative_path: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, relative_path)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative_path)


DATA_DIR = os.path.join(get_base_path(), "data")
BACKUP_DIR = os.path.join(get_base_path(), "backups")
DB_PATH = os.path.join(DATA_DIR, "sr_database.db")

_write_lock = threading.RLock()
_pool_lock = threading.Lock()
_pg_pool = None
_pg_pool_dsn = ""
_backup_thread = None
_stop_backup = threading.Event()
_current_username = contextvars.ContextVar("sr_current_username", default="LAN_User")


def get_database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def is_postgres_enabled() -> bool:
    return bool(get_database_url())


def _mask_dsn(dsn: str) -> str:
    try:
        p = urlparse(dsn)
        host = p.hostname or "postgresql"
        db = (p.path or "").lstrip("/") or "neondb"
        return f"postgresql://{host}/{db}"
    except Exception:
        return "postgresql://neon"


def _get_pg_pool():
    global _pg_pool, _pg_pool_dsn
    dsn = get_database_url()
    if not dsn:
        return None
    if psycopg2 is None:
        raise RuntimeError("DATABASE_URL is set, but 'psycopg2-binary' is not installed.")
    with _pool_lock:
        if _pg_pool is not None and _pg_pool_dsn == dsn:
            return _pg_pool
        if _pg_pool is not None:
            try:
                _pg_pool.closeall()
            except Exception:
                pass
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=dsn)
        _pg_pool_dsn = dsn
        return _pg_pool


class _PgRow:
    """sqlite3.Row-compatible wrapper for psycopg2 tuple rows."""

    __slots__ = ("_cols", "_vals", "_col_map")

    def __init__(self, cols: tuple[str, ...], vals: tuple):
        self._cols = cols
        self._vals = vals
        cmap: dict[str, int] = {}
        for idx, name in enumerate(cols):
            if name not in cmap:
                cmap[name] = idx
            low = name.lower()
            if low not in cmap:
                cmap[low] = idx
        self._col_map = cmap

    def keys(self) -> list[str]:
        return list(self._cols)

    def get(self, key: str, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._vals[key]
        if isinstance(key, str):
            idx = self._col_map.get(key)
            if idx is None:
                idx = self._col_map.get(key.lower())
            if idx is None:
                raise KeyError(key)
            return self._vals[idx]
        raise TypeError(f"Row indices must be integers, slices, or strings, not {type(key).__name__}")

    def __iter__(self):
        return iter(self._vals)

    def __len__(self) -> int:
        return len(self._vals)

    def __repr__(self) -> str:
        return f"_PgRow({dict(self)!r})"


_PRAGMA_TABLE_INFO_RE = re.compile(
    r'^\s*PRAGMA\s+table_info\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)\s*;?\s*$',
    re.IGNORECASE,
)
_PRAGMA_JOURNAL_RE = re.compile(r'^\s*PRAGMA\s+journal_mode\b', re.IGNORECASE)
_PRAGMA_ANY_RE = re.compile(r'^\s*PRAGMA\b', re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\b', re.IGNORECASE)
_CREATE_VIEW_IF_RE = re.compile(r'\bCREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\b', re.IGNORECASE)
_AUTOINC_RE = re.compile(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b', re.IGNORECASE)
_SQLITE_MASTER_RE = re.compile(r'\bFROM\s+sqlite_master\b', re.IGNORECASE)


def _replace_unquoted_sql(sql: str, has_params: bool) -> str:
    """Convert SQLite '?' placeholders to '%s' outside quotes and escape literal '%' when params exist."""
    out = []
    in_single = False
    in_double = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" and not in_double:
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            if in_double and i + 1 < n and sql[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        elif ch == "%" and has_params:
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _rewrite_ddl_types_outside_quotes(sql: str) -> str:
    """Replace SQLite REAL column type with DOUBLE PRECISION outside quoted identifiers/strings."""
    parts = re.split(r"""('(?:''|[^'])*'|"(?:""|[^"])*")""", sql)
    for idx in range(0, len(parts), 2):
        parts[idx] = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', parts[idx], flags=re.IGNORECASE)
    return "".join(parts)


def translate_sql_for_pg(sql: str, params=()) -> tuple[str, tuple | None, list | None]:
    """Translate a SQLite-flavored SQL statement for psycopg2 PostgreSQL execution.

    Returns (pg_sql, pg_params, synthetic_rows_or_None).
    """
    m_ti = _PRAGMA_TABLE_INFO_RE.match(sql)
    if m_ti:
        tbl = m_ti.group(1)
        pg_sql = (
            "SELECT "
            "(c.ordinal_position - 1) AS cid, "
            "c.column_name AS name, "
            "UPPER(c.data_type) AS type, "
            "CASE WHEN c.is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
            "c.column_default AS dflt_value, "
            "CASE WHEN pk.attname IS NOT NULL THEN 1 ELSE 0 END AS pk "
            "FROM information_schema.columns c "
            "LEFT JOIN ("
            "  SELECT a.attname "
            "  FROM pg_index i "
            "  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "  WHERE i.indrelid = to_regclass(%s) AND i.indisprimary"
            ") pk ON pk.attname = c.column_name "
            "WHERE c.table_schema = 'public' AND c.table_name = %s "
            "ORDER BY c.ordinal_position"
        )
        return pg_sql, (f'"{tbl}"', tbl), None

    if _PRAGMA_JOURNAL_RE.match(sql):
        return "", None, [("POSTGRESQL",)]

    if _PRAGMA_ANY_RE.match(sql):
        return "", None, []

    s = sql
    if _SQLITE_MASTER_RE.search(s):
        s = _SQLITE_MASTER_RE.sub(
            "FROM (SELECT table_name AS name, CASE WHEN table_type = 'VIEW' THEN 'view' ELSE 'table' END AS type "
            "FROM information_schema.tables WHERE table_schema = 'public') AS sqlite_master",
            s,
        )

    if _CREATE_VIEW_IF_RE.search(s):
        s = _CREATE_VIEW_IF_RE.sub("CREATE OR REPLACE VIEW", s)

    if _AUTOINC_RE.search(s):
        s = _AUTOINC_RE.sub("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", s)

    upper_head = s.lstrip().upper()
    if upper_head.startswith("CREATE TABLE") or upper_head.startswith("ALTER TABLE"):
        s = _rewrite_ddl_types_outside_quotes(s)

    if _INSERT_OR_IGNORE_RE.match(s):
        s = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", s, count=1).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    param_tuple = tuple(params) if params else ()
    has_params = len(param_tuple) > 0
    pg_sql = _replace_unquoted_sql(s, has_params=has_params)
    return pg_sql, (param_tuple if has_params else None), None


class _PgCursor:
    """sqlite3.Cursor-compatible wrapper around psycopg2 cursor."""

    def __init__(self, raw_cursor):
        self._cur = raw_cursor
        self._synthetic_rows: list | None = None
        self._synthetic_idx = 0
        self._synthetic_cols: tuple[str, ...] = ()

    def execute(self, sql: str, params=()):
        self._synthetic_rows = None
        self._synthetic_idx = 0
        pg_sql, pg_params, synthetic = translate_sql_for_pg(sql, params)
        if synthetic is not None:
            self._synthetic_rows = synthetic
            self._synthetic_cols = ("journal_mode",) if synthetic else ()
            return self
        if pg_params is not None:
            self._cur.execute(pg_sql, pg_params)
        else:
            self._cur.execute(pg_sql)
        return self

    def executemany(self, sql: str, seq_of_params):
        payload = [tuple(p) for p in seq_of_params]
        if not payload:
            return self
        pg_sql, _, _ = translate_sql_for_pg(sql, payload[0])
        psycopg2.extras.execute_batch(self._cur, pg_sql, payload, page_size=200)
        return self

    def _wrap_row(self, row):
        if row is None:
            return None
        if isinstance(row, _PgRow):
            return row
        desc = self._cur.description
        if not desc:
            return row
        cols = tuple(d[0] for d in desc)
        return _PgRow(cols, tuple(row))

    def fetchone(self):
        if self._synthetic_rows is not None:
            if self._synthetic_idx >= len(self._synthetic_rows):
                return None
            row = self._synthetic_rows[self._synthetic_idx]
            self._synthetic_idx += 1
            return _PgRow(self._synthetic_cols, tuple(row)) if self._synthetic_cols else row
        return self._wrap_row(self._cur.fetchone())

    def fetchall(self):
        if self._synthetic_rows is not None:
            rest = self._synthetic_rows[self._synthetic_idx:]
            self._synthetic_idx = len(self._synthetic_rows)
            if not rest:
                return []
            return [_PgRow(self._synthetic_cols, tuple(r)) if self._synthetic_cols else r for r in rest]
        rows = self._cur.fetchall()
        if not rows:
            return []
        desc = self._cur.description
        if not desc:
            return rows
        cols = tuple(d[0] for d in desc)
        return [_PgRow(cols, tuple(r)) for r in rows]

    def fetchmany(self, size: int = 1):
        if self._synthetic_rows is not None:
            end = self._synthetic_idx + size
            chunk = self._synthetic_rows[self._synthetic_idx:end]
            self._synthetic_idx = min(end, len(self._synthetic_rows))
            return [_PgRow(self._synthetic_cols, tuple(r)) if self._synthetic_cols else r for r in chunk]
        rows = self._cur.fetchmany(size)
        if not rows:
            return []
        desc = self._cur.description
        if not desc:
            return rows
        cols = tuple(d[0] for d in desc)
        return [_PgRow(cols, tuple(r)) for r in rows]

    @property
    def description(self):
        if self._synthetic_rows is not None:
            return [(c, None, None, None, None, None, None) for c in self._synthetic_cols] if self._synthetic_cols else None
        return self._cur.description

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", -1)

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        return iter(self.fetchall())


class _PgConnection:
    """sqlite3.Connection-compatible wrapper around pooled psycopg2 connections."""

    def __init__(self, raw_conn, pool=None):
        self._conn = raw_conn
        self._pool = pool
        self.row_factory = None

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor())

    def execute(self, sql: str, params=()) -> _PgCursor:
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, seq_of_params) -> _PgCursor:
        return self.cursor().executemany(sql, seq_of_params)

    def executescript(self, sql_script: str) -> None:
        buf = ""
        for line in sql_script.splitlines(keepends=True):
            stripped = line.strip()
            if not buf and (not stripped or stripped.startswith("--")):
                continue
            buf += line
            if sqlite3.complete_statement(buf):
                stmt = buf.strip()
                buf = ""
                if not stmt:
                    continue
                self.execute(stmt)
        if buf.strip():
            self.execute(buf.strip())

    def commit(self) -> None:
        if not self._conn.autocommit:
            self._conn.commit()

    def rollback(self) -> None:
        try:
            if not self._conn.autocommit:
                self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        if self._pool is not None:
            try:
                if not self._conn.closed and not self._conn.autocommit:
                    self._conn.rollback()
            except Exception:
                pass
            try:
                self._pool.putconn(self._conn, close=bool(self._conn.closed))
            except Exception:
                pass
        else:
            try:
                self._conn.close()
            except Exception:
                pass


def set_current_username(username: str):
    return _current_username.set((username or "").strip() or "LAN_User")


def reset_current_username(token) -> None:
    _current_username.reset(token)


def get_current_username() -> str:
    return _current_username.get()


_PUMP_VIEWS_SQL = """
CREATE VIEW IF NOT EXISTS "vw_new_dh_pump_data" AS
SELECT
    "_rowid_pk",
    "WellName",
    "WellRunDate",
    "PumpSN",
    "Pumpdesignation",
    "Vendor",
    "Pumpsize",
    "PumpType",
    "Field1" AS "BarrelLength",
    "Extension",
    "PumpLength",
    "PlugSeat",
    "PlungerClearance",
    "PumpCOND",
    "SV",
    "DSV",
    "TV",
    "DTV",
    "Openplunger",
    "StartDate",
    "FailureDate",
    "PumpLifetime",
    "PumpNo",
    "notes",
    "Both"
FROM "tblpumpdata"
ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC;

CREATE VIEW IF NOT EXISTS "vw_pump_teardown_data" AS
SELECT
    "_rowid_pk",
    "WellName",
    "WellRunDate",
    "PumpSN",
    "Pumpdesignation",
    "Vendor",
    "PumpCOND",
    "StartDate",
    "FailureDate",
    "PumpLifetime",
    "pumpreportdate",
    "welltype",
    "pumpinspectioncond",
    "barrelcond", "barreldefect", "barrelapparentfailure",
    "rodguidecond", "rodguidedefect", "rodguideapparentfailure",
    "barrelconnectorcond", "barrelconnectordefect", "barrelconnectorapparentfailure",
    "topextcond", "topextdefect", "topextapparentfailure",
    "botextcond", "botextdefect", "botextapparentfailure",
    "SVcond", "SVdefect", "SVapparentfailure",
    "SVballcond", "SVseatcond", "SVballseattest", "SVballseatcomment",
    "DSVcond", "DSVdefect", "DSVapparentfailure",
    "DSVballcond", "DSVseatcond", "DSVballseattest", "DSVballseatcomment",
    "coup/pseatcond", "coup/pseatdefect", "coup/pseatapparentfailure",
    "plungercond", "plungerdefect", "plungerapparentfailure",
    "vrodcond", "vroddefect", "vrodapparentfailure",
    "vrodbushingcond", "vrodbushingdefect", "vrodbushingapparentfailure",
    "vrodnutcond", "vrodnutdefect", "vrodnutapparentfailure",
    "oplungercond", "oplungerdefect", "oplungerapparentfailure",
    "TVcond", "TVdefect", "TVapparentfailure",
    "TVballcond", "TVseatcond", "TVballseattest", "TVballseatcomment",
    "DTVcond", "DTVdefect", "DTVapparentfailure",
    "DTVballcond", "DTVseatcond", "DTVballseattest", "DTVballseatcomment",
    "TVpseatcond", "TVpseatdefect", "TVpseatapparentfailure",
    "hdownbushcond", "hdownbushdefect", "hdownbushapparentfailure",
    "mandrelcond", "mandreldefect", "mandrelapparentfailure",
    "sleevecond", "sleevedefect", "sleeveapparentfailure",
    "hdownringcond", "hdownringdefect", "hdownringapparentfailure",
    "inspectby", "preparedby", "locationonpc", "comments", "Both"
FROM "tblpumpdata"
WHERE TRIM(COALESCE("pumpreportdate", '')) <> ''
   OR TRIM(COALESCE("inspectby", '')) <> ''
   OR TRIM(COALESCE("pumpinspectioncond", '')) <> ''
ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC;
"""


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _borrow_pg_connection(autocommit: bool = False):
    pool = _get_pg_pool()
    for _ in range(2):
        raw_conn = pool.getconn()
        try:
            if raw_conn.closed:
                pool.putconn(raw_conn, close=True)
                continue
            raw_conn.autocommit = True
            with raw_conn.cursor() as cur:
                cur.execute("SELECT 1")
            raw_conn.autocommit = autocommit
            return _PgConnection(raw_conn, pool=pool)
        except Exception:
            try:
                pool.putconn(raw_conn, close=True)
            except Exception:
                pass
    raw_conn = psycopg2.connect(get_database_url())
    raw_conn.autocommit = autocommit
    return _PgConnection(raw_conn, pool=None)


def get_connection(db_path: str = None, autocommit: bool = False):
    ensure_dirs()
    if db_path is None and is_postgres_enabled():
        return _borrow_pg_connection(autocommit=autocommit)

    path = db_path or DB_PATH
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 15000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -16000;")
    return conn


@contextmanager
def db_session(write: bool = False):
    pg = is_postgres_enabled()
    if write and not pg:
        _write_lock.acquire()
    conn = get_connection(autocommit=(pg and not write))
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.close()
        if write and not pg:
            _write_lock.release()


def _to_iso_date_str(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return s
    # Already strict YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    s_date = s.split(" ")[0].strip()
    for candidate in (s, s_date):
        for fmt in (
            "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d", "%Y/%m/%d %H:%M:%S",
            "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%y", "%m/%d/%y %H:%M:%S",
            "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y",
        ):
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return s


def normalize_all_dates(conn):
    """Standardize legacy Access dates (mm/dd/yyyy, dd-Mon-yy, etc.) to YYYY-MM-DD across all tables."""
    table_date_cols = {
        "tblwelldata": ["WellRunDate"],
        "tblpumpdata": ["WellRunDate", "StartDate", "FailureDate", "pumpreportdate"],
        "tblTBGdata": ["WellRunDate", "TBGStartDate", "TBGFailureDate"],
        "tblTBGdatanew": ["WellRunDate"],
        "tblrodstringdata1": ["WellRunDate"],
        "tblrodstringdata2": ["WellRunDate", "PolishedRodstartdate"],
        "tblrodsdatanew": ["WellRunDate"],
        "tblrodstracking1": ["WellRunDate", "Rodsfailuredate"],
        "tblrodstracking2": ["WellRunDate", "Rodsfailuredate"],
        "tblwoverdata": ["WellRunDate", "SDDate", "WOverDate", "CLTestDate"],
        "tblwearcommentsdata1": ["WellRunDate"],
        "tblwearcommentsdata2": ["WellRunDate"],
        "tblwearcommentsdatanew": ["WellRunDate"],
        "tbldatanew": ["WellRunDate"],
    }
    for tbl, cols in table_date_cols.items():
        try:
            existing_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
        except Exception:
            continue
        pk_col = "id" if "id" in existing_cols else ("_rowid_pk" if "_rowid_pk" in existing_cols else None)
        if not pk_col:
            continue
        valid_cols = [c for c in cols if c in existing_cols]
        if not valid_cols:
            continue
        has_both = "Both" in existing_cols and "WellName" in existing_cols and "WellRunDate" in existing_cols
        sel_cols = [pk_col] + valid_cols + (["WellName", "Both"] if has_both else [])
        col_sql = ", ".join(f'"{c}"' for c in sel_cols)
        rows = conn.execute(f'SELECT {col_sql} FROM "{tbl}"').fetchall()
        for r in rows:
            updates = {}
            for c in valid_cols:
                old_v = r[c]
                if old_v is not None and str(old_v).strip():
                    new_v = _to_iso_date_str(old_v)
                    if new_v != old_v:
                        updates[c] = new_v
            if has_both and "WellRunDate" in updates:
                wn = str(r["WellName"] or "").strip()
                updates["Both"] = f"{wn} {updates['WellRunDate']}".strip()
            if updates:
                set_sql = ", ".join(f'"{k}"=?' for k in updates)
                conn.execute(
                    f'UPDATE "{tbl}" SET {set_sql} WHERE "{pk_col}"=?',
                    list(updates.values()) + [r[pk_col]],
                )


def ensure_pump_views(conn):
    """Create normalized SQL views matching legacy Access pump queries."""
    conn.executescript(_PUMP_VIEWS_SQL)


def init_db(seed_if_empty: bool = True):
    ensure_dirs()
    schema_file = get_resource_path(os.path.join("app", "schema.sql"))
    with open(schema_file, "r", encoding="utf-8") as sf:
        schema_sql = sf.read()
    with db_session(write=True) as conn:
        conn.executescript(schema_sql)
        ensure_pump_views(conn)
        # Always ensure any newly-added dropdown field keys are seeded without overwriting existing admin edits
        from app.seed_data import seed_dropdowns
        seed_dropdowns(conn)
        from app.auth import ensure_auth_schema
        ensure_auth_schema(conn)
        if seed_if_empty:
            if conn.execute('SELECT COUNT(*) FROM "tblwells"').fetchone()[0] == 0:
                from app.seed_data import seed_database
                seed_database(conn)
        normalize_all_dates(conn)


def create_backup(max_backups: int = 30, tag: str = "") -> str:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{tag}" if tag else ""
    dest_path = os.path.join(BACKUP_DIR, f"sr_database_{ts}{suffix}.db")
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(BACKUP_DIR, f"sr_database_{ts}{suffix}_{counter}.db")
        counter += 1
    with _write_lock:
        if is_postgres_enabled():
            schema_file = get_resource_path(os.path.join("app", "schema.sql"))
            with open(schema_file, "r", encoding="utf-8") as sf:
                schema_sql = sf.read()
            dst = sqlite3.connect(dest_path)
            try:
                dst.executescript(schema_sql)
                dst.executescript(_PUMP_VIEWS_SQL)
                with db_session() as pg_conn:
                    tables = [
                        r[0] for r in pg_conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                        ).fetchall()
                    ]
                    for t in tables:
                        dst_cols = {r[1] for r in dst.execute(f'PRAGMA table_info("{t}")').fetchall()}
                        if not dst_cols:
                            continue
                        cur = pg_conn.execute(f'SELECT * FROM "{t}"')
                        src_cols = [d[0] for d in cur.description if d[0] in dst_cols]
                        if not src_cols:
                            continue
                        rows = cur.fetchall()
                        if rows:
                            col_sql = ", ".join(f'"{c}"' for c in src_cols)
                            ph = ", ".join("?" for _ in src_cols)
                            dst.executemany(
                                f'INSERT OR IGNORE INTO "{t}" ({col_sql}) VALUES ({ph})',
                                [tuple(r[c] for c in src_cols) for r in rows],
                            )
                dst.commit()
            finally:
                dst.close()
        else:
            src = get_connection()
            dst = sqlite3.connect(dest_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "sr_database_*.db")))
    if len(backups) > max_backups:
        for old in backups[:-max_backups]:
            if os.path.abspath(old) == os.path.abspath(dest_path):
                continue
            try:
                os.remove(old)
            except OSError:
                pass
    return dest_path


def list_backups():
    ensure_dirs()
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "sr_database_*.db")), reverse=True)
    return [
        {
            "filename": os.path.basename(fp),
            "size_kb": round(os.stat(fp).st_size / 1024, 1),
            "modified": datetime.fromtimestamp(os.stat(fp).st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for fp in files
    ]


def start_backup_scheduler(interval_hours: float = 24.0):
    global _backup_thread
    if is_postgres_enabled() or os.environ.get("K_SERVICE"):
        return
    if _backup_thread and _backup_thread.is_alive():
        return
    def _loop():
        interval_sec = max(60, int(interval_hours * 3600))
        while not _stop_backup.wait(interval_sec):
            try:
                create_backup()
            except Exception as e:
                print(f"[Backup Scheduler] Error: {e}")
    _backup_thread = threading.Thread(target=_loop, daemon=True, name="SQLiteBackupScheduler")
    _backup_thread.start()


def stop_backup_scheduler():
    _stop_backup.set()


def export_new_pump_table_excel_bytes(well: str = "") -> bytes:
    """Export the normalized New DH Pump table (vw_new_dh_pump_data) + any active custom pump fields to styled .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "New_DH_Pump_Data"

    hdr_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    hdr_fill = PatternFill(start_color="0F2544", end_color="0F2544", fill_type="solid")
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    cell_font = Font(name="Calibri", size=10, color="1E293B")
    thin = Side(style="thin", color="CBD5E1")
    bdr = Border(left=thin, right=thin, top=thin, bottom=thin)

    with db_session() as conn:
        ensure_pump_views(conn)
        sql = 'SELECT * FROM "vw_new_dh_pump_data"'
        params = []
        if well and well.strip():
            sql += ' WHERE "WellName" = ?'
            params.append(well.strip())
        cur = conn.execute(sql, params)
        base_cols = [d[0] for d in cur.description if d[0] not in ("_rowid_pk", "Both")]
        rows = [dict(r) for r in cur.fetchall()]

        # Include any active custom fields defined on the "pumps" page (e.g., Unswept Length)
        custom_fields = []
        custom_map = {}
        try:
            custom_fields = [
                dict(r) for r in conn.execute(
                    'SELECT "id", "label" FROM "app_custom_fields" '
                    'WHERE "target_page"=? AND "active"=1 ORDER BY "sort_order", "id"',
                    ("pumps",),
                ).fetchall()
            ]
            if custom_fields:
                fids = [cf["id"] for cf in custom_fields]
                ph = ",".join("?" * len(fids))
                for vr in conn.execute(
                    f'SELECT "field_id", "WellName", "WellRunDate", "value" '
                    f'FROM "app_custom_values" WHERE "field_id" IN ({ph})',
                    fids,
                ).fetchall():
                    key = (str(vr["WellName"] or "").strip(), _to_iso_date_str(vr["WellRunDate"]) or "")
                    custom_map.setdefault(key, {})[vr["field_id"]] = vr["value"]
        except Exception:
            custom_fields = []

    headers = base_cols + [cf["label"] for cf in custom_fields]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col_idx)
        c.font = hdr_font
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = bdr
    ws.row_dimensions[1].height = 22

    for idx, r in enumerate(rows, start=2):
        rkey = (str(r.get("WellName") or "").strip(), _to_iso_date_str(r.get("WellRunDate")) or "")
        cf_vals = custom_map.get(rkey, {})
        row_vals = [r.get(c) for c in base_cols] + [cf_vals.get(cf["id"], "") for cf in custom_fields]
        ws.append(row_vals)
        is_zebra = (idx % 2 == 1)
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=idx, column=col_idx)
            c.font = cell_font
            c.border = bdr
            if is_zebra:
                c.fill = zebra_fill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(rows) + 1)}"

    # Auto-fit column widths
    for col_idx in range(1, len(headers) + 1):
        col_letter = get_column_letter(col_idx)
        max_len = max(
            (len(str(ws.cell(row=r_i, column=col_idx).value or "")) for r_i in range(1, min(len(rows) + 2, 502))),
            default=10,
        )
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 11), 42)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_db_to_excel_bytes(include_program: bool = False) -> bytes:
    """Export tables + normalized pump views into a multi-sheet Excel (.xlsx) workbook.

    By default (include_program=False), program-internal tables (app_*) are excluded so
    end-user backups contain only operational well/equipment tables and views.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    hdr_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    hdr_fill = PatternFill(start_color="0F2544", end_color="0F2544", fill_type="solid")
    app_filter = "AND name <> 'app_sessions'" if include_program else "AND name NOT LIKE 'app_%'"

    with db_session() as conn:
        ensure_pump_views(conn)
        objects = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                f"{app_filter} "
                "ORDER BY CASE WHEN name LIKE 'vw_%' THEN 0 ELSE 1 END, name"
            ).fetchall()
        ]
        for obj in objects:
            ws = wb.create_sheet(title=obj[:31])
            cur = conn.execute(f'SELECT * FROM "{obj}"')
            cols = [d[0] for d in cur.description if not (obj == "app_users" and d[0] == "password_hash")]
            ws.append(cols)
            for cell in ws[1]:
                cell.font = hdr_font
                cell.fill = hdr_fill
            ws.freeze_panes = "A2"
            for row in cur.fetchall():
                ws.append([row[c] for c in cols])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_db_to_csv_zip_bytes(include_program: bool = False) -> bytes:
    """Export tables + normalized pump views as UTF-8 CSV files bundled inside a .zip archive.

    By default (include_program=False), program-internal tables (app_*) are excluded.
    """
    buf = io.BytesIO()
    app_filter = "AND name <> 'app_sessions'" if include_program else "AND name NOT LIKE 'app_%'"
    with db_session() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ensure_pump_views(conn)
        objects = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                f"{app_filter} ORDER BY name"
            ).fetchall()
        ]
        for obj in objects:
            cur = conn.execute(f'SELECT * FROM "{obj}"')
            cols = [d[0] for d in cur.description if not (obj == "app_users" and d[0] == "password_hash")]
            sio = io.StringIO()
            writer = csv.writer(sio)
            writer.writerow(cols)
            for row in cur.fetchall():
                writer.writerow(["" if row[c] is None else row[c] for c in cols])
            zf.writestr(f"{obj}.csv", sio.getvalue().encode("utf-8-sig"))
    return buf.getvalue()


def get_db_status() -> dict:
    pg = is_postgres_enabled()
    with db_session() as conn:
        if pg:
            mode = "NEON POSTGRESQL"
            try:
                sz_row = conn.execute("SELECT ROUND(pg_database_size(current_database()) / 1048576.0, 2)").fetchone()
                size_mb = float(sz_row[0]) if sz_row and sz_row[0] is not None else 0.0
            except Exception:
                size_mb = 0.0
        else:
            mode_row = conn.execute("PRAGMA journal_mode;").fetchone()
            mode = str(mode_row[0]).upper() if mode_row and mode_row[0] else "WAL"
            size_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 2) if os.path.exists(DB_PATH) else 0.0
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    return {
        "backend": "postgresql" if pg else "sqlite",
        "db_path": _mask_dsn(get_database_url()) if pg else DB_PATH,
        "journal_mode": mode,
        "size_mb": size_mb,
        "table_count": len(tables),
        "counts": counts,
        "backups": list_backups()[:10],
    }


def log_audit(conn, action: str, table_name: str, record_ref: str, username: str = None):
    who = (username or "").strip() or get_current_username()
    conn.execute('INSERT INTO "app_audit_log" ("username", "action", "table_name", "record_ref") VALUES (?, ?, ?, ?)', (who, action, table_name, record_ref))
