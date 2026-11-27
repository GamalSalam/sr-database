"""Universal Access (.accdb / .mdb) Importer used by both CLI and Admin Web UI.
Automatically tries:
1) Windows Access COM (64-bit & 32-bit SysWOW64 PowerShell) — works with 32-bit or 64-bit Office!
2) `pyodbc` ODBC driver (auto-detected from installed ODBC drivers)
3) `mdb-export` (`mdbtools` on Linux/macOS)
"""
import os
import csv
import io
import shutil
import tempfile
import datetime
import subprocess
from app.database import init_db, db_session, create_backup, DB_PATH, log_audit

TABLES = [
    "tblwells", "tblwelldata", "tblrodgrades", "tblpumpdata",
    "tblTBGdata", "tblTBGdatanew", "tblrodstringdata1", "tblrodstringdata2",
    "tblrodsdatanew", "tblrodstracking1", "tblrodstracking2", "tblwoverdata",
    "tblwearcommentsdata1", "tblwearcommentsdata2", "tblwearcommentsdatanew", "tbldatanew",
]


def _norm(val):
    if val is None or val == "":
        return None
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    for candidate in (s, s.split(" ")[0] if ("/" in s and ":" in s) else s):
        for fmt in (
            "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%y %H:%M:%S", "%m/%d/%Y", "%m/%d/%y",
            "%Y-%m-%d %H:%M:%S", "%d-%b-%y", "%d-%b-%Y",
        ):
            try:
                return datetime.datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return s


def _export_via_powershell_com(accdb_path: str, out_dir: str, ps_exe: str = "powershell") -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Not Windows"
    abs_db = os.path.abspath(accdb_path).replace("'", "''")
    abs_out = os.path.abspath(out_dir).replace("'", "''")
    tbl_list = ",".join(f"'{t}'" for t in TABLES)
    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    $acc = $null
    try {{
        $acc = New-Object -ComObject Access.Application
        $acc.Visible = $false
        $acc.OpenCurrentDatabase('{abs_db}', $false)
        $tables = @({tbl_list})
        foreach ($t in $tables) {{
            try {{
                $outFile = Join-Path '{abs_out}' ($t + '.csv')
                $acc.DoCmd.TransferText(2, $null, $t, $outFile, $true)
            }} catch {{}}
        }}
    }} finally {{
        if ($acc -ne $null) {{
            try {{ $acc.CloseCurrentDatabase() }} catch {{}}
            try {{ $acc.Quit() }} catch {{}}
        }}
    }}
    """
    try:
        res = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=180
        )
        if res.returncode == 0 and any(os.scandir(out_dir)):
            return True, ""
        return False, (res.stderr or res.stdout or "No CSV exported").strip()
    except Exception as e:
        return False, str(e)


def _find_access_odbc_driver() -> str:
    import pyodbc
    drivers = [d for d in pyodbc.drivers() if "access" in d.lower() and ("accdb" in d.lower() or "mdb" in d.lower())]
    return drivers[0] if drivers else ""


def _read_via_pyodbc(accdb_path: str, table: str, driver_name: str):
    import pyodbc
    conn_str = f"DRIVER={{{driver_name}}};DBQ={os.path.abspath(accdb_path)};"
    with pyodbc.connect(conn_str) as c:
        cur = c.cursor()
        cur.execute(f"SELECT * FROM [{table}]")
        return [d[0] for d in cur.description], cur.fetchall()


def _read_via_mdbtools(accdb_path: str, table: str):
    res = subprocess.run(
        ["mdb-export", "-D", "%Y-%m-%d", accdb_path, table],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "mdb-export failed")
    rows = list(csv.reader(io.StringIO(res.stdout)))
    return (rows[0], rows[1:]) if rows else ([], [])


def import_accdb_file(accdb_path: str, source_label: str = "AdminUI") -> dict:
    if not os.path.exists(accdb_path):
        raise FileNotFoundError(f"Access file not found: {accdb_path}")

    init_db(seed_if_empty=False)
    backup_name = os.path.basename(create_backup()) if os.path.exists(DB_PATH) else None

    tmp_csv_dir = tempfile.mkdtemp(prefix="sr_acc_csv_")
    used_com = False
    method = "unknown"
    odbc_driver = ""

    try:
        if os.name == "nt":
            ok, err64 = _export_via_powershell_com(accdb_path, tmp_csv_dir, "powershell")
            if ok:
                used_com = True
                method = "Windows Access COM (64-bit)"
            else:
                ps32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe")
                ok32, err32 = (False, "")
                if os.path.exists(ps32):
                    ok32, err32 = _export_via_powershell_com(accdb_path, tmp_csv_dir, ps32)
                if ok32:
                    used_com = True
                    method = "Windows Access COM (32-bit Office bridge)"
                else:
                    try:
                        odbc_driver = _find_access_odbc_driver()
                    except Exception:
                        odbc_driver = ""
                    if not odbc_driver:
                        raise RuntimeError(
                            "Could not open .accdb: neither Microsoft Access COM nor a 64-bit Access ODBC driver is available on this PC. "
                            f"(64-bit COM: {err64[:120]} | 32-bit COM: {err32[:120]}). "
                            "Install 'Microsoft Access Database Engine 2016 Redistributable (64-bit)' or Microsoft Access."
                        )
                    method = f"Windows pyodbc ({odbc_driver})"
        elif shutil.which("mdb-export"):
            method = "Linux mdbtools (mdb-export)"
        else:
            raise RuntimeError("No Access reader found. Install mdbtools on Linux or Microsoft Access / ODBC driver on Windows.")

        results = []
        total_rows = 0

        with db_session(write=True) as sq:
            for t in TABLES:
                try:
                    if used_com:
                        csv_file = os.path.join(tmp_csv_dir, f"{t}.csv")
                        if not os.path.exists(csv_file):
                            results.append({"table": t, "status": "SKIP", "rows": 0, "detail": "Not in export"})
                            continue
                        with open(csv_file, "r", encoding="utf-8-sig", errors="replace") as cf:
                            parsed = list(csv.reader(cf))
                        cols = parsed[0] if parsed else []
                        rows = parsed[1:] if len(parsed) > 1 else []
                    elif os.name == "nt":
                        cols, rows = _read_via_pyodbc(accdb_path, t, odbc_driver)
                    else:
                        cols, rows = _read_via_mdbtools(accdb_path, t)
                except Exception as e:
                    results.append({"table": t, "status": "SKIP", "rows": 0, "detail": str(e)[:120]})
                    continue

                sq_cols = {r[1].lower(): r[1] for r in sq.execute(f'PRAGMA table_info("{t}")').fetchall()}
                matched = [(i, sq_cols[c.lower()]) for i, c in enumerate(cols) if c.lower() in sq_cols]
                if not matched:
                    results.append({"table": t, "status": "WARN", "rows": 0, "detail": "No matching columns"})
                    continue

                sq.execute(f'DELETE FROM "{t}"')
                col_sql = ", ".join(f'"{c}"' for _, c in matched)
                q_sql = ", ".join("?" for _ in matched)
                insert_sql = f'INSERT INTO "{t}" ({col_sql}) VALUES ({q_sql})'
                payload = [tuple(_norm(r[i]) if i < len(r) else None for i, _ in matched) for r in rows]
                sq.executemany(insert_sql, payload)
                total_rows += len(payload)
                results.append({"table": t, "status": "OK", "rows": len(payload), "detail": "Imported"})

            log_audit(sq, "IMPORT_ACCDB", "ALL_TABLES", f"{os.path.basename(accdb_path)} ({total_rows} rows)", source_label)

        return {
            "method": method,
            "backup_file": backup_name,
            "total_rows": total_rows,
            "tables": results,
        }
    finally:
        shutil.rmtree(tmp_csv_dir, ignore_errors=True)