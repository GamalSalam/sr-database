import os
import tempfile
from datetime import datetime
from urllib.parse import quote
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import (
    init_db, db_session, create_backup, get_db_status, list_backups,
    start_backup_scheduler, stop_backup_scheduler, get_resource_path, log_audit,
    export_db_to_excel_bytes, export_db_to_csv_zip_bytes, export_new_pump_table_excel_bytes,
    get_current_username,
)
from app.calculations import calculate_well_summary
from app.excel_export import build_report_workbook
from app.accdb_importer import import_accdb_file
from app.backup_restore import (
    resolve_server_backup_path,
    restore_from_db_file,
    restore_from_uploaded_backup,
)
from app.seed_data import DROPDOWN_FIELD_REGISTRY
from app.custom_fields import router as custom_fields_router, get_custom_values_map, append_custom_fields_to_xlsx
from app.insights import router as insights_router, get_dashboard_runs
from app.auth import router as auth_router, AuthMiddleware, SESSION_COOKIE_NAME

REPORT_DEFINITIONS = {
    "rptwellboresketch": {
        "title": "Wellbore Sketch Report",
        "category": "Completion & RIH",
        "desc": "Unified Wellbore Sketch with pumpjack schematic, Well Data, Pump Data, Tubing + Rod String table, TAC Set, Weight Indicator, KTH Flange, Comments, and Depths (ORKB).",
    },
    "rptwoverwwear": {
        "title": "Work Over Report (With Rod Wear Analysis)",
        "category": "Workover",
        "desc": "Full Workover Report: POOH Rigs, Rod String Weights/Overpull, TAC Unset, Rig Situation, Well-Failure Reason, and Pulled Rod Wear Table + Pitting.",
    },
    "rptwoverwowear": {
        "title": "Work Over Report (Without Wear Comments)",
        "category": "Workover",
        "desc": "Concise Workover Report without the rod wear breakdown table.",
    },
    "rptpumpreport": {
        "title": "Pump Tear Down and Inspection Report",
        "category": "Workshop Inspection",
        "desc": "Full workshop teardown report: 3-column header, Barrel Assembly, Plunger Assembly, Hold Down Assembly, General Comments, and Prepared/Inspected By.",
    },
}


# Which custom-field pages feed each report
REPORT_CUSTOM_PAGES = {
    "rptwellboresketch": ["tbg", "rods", "pumps"],
    "rptwoverwwear": ["workover", "wear"],
    "rptwoverwowear": ["workover"],
    "rptpumpreport": ["pumps", "teardown"],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(seed_if_empty=True)
    start_backup_scheduler(interval_hours=24.0)
    yield
    stop_backup_scheduler()


app = FastAPI(title="SR Database — Standalone LAN Server v3", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=get_resource_path(os.path.join("app", "static"))), name="static")
templates = Jinja2Templates(directory=get_resource_path(os.path.join("app", "templates")))
app.include_router(auth_router)
app.include_router(custom_fields_router)  # Enhancement #1: dynamic custom fields
app.include_router(insights_router)       # Enhancements #2/#3: history panel API + dashboard rules

def _parse_any_date(val) -> Optional[datetime]:
    """Parse any Access, ISO, or text date string (including '9/8/2021 0:00:00' or '16-May-24')."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    # Strip trailing time like '0:00:00', '00:00:00', '12:00:00 AM'
    s_date = s.split(" ")[0].strip()
    for candidate in (s, s_date):
        for fmt in (
            "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
            "%m/%d/%y", "%m/%d/%y %H:%M:%S",
            "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def to_iso_date(val) -> str:
    """Convert any stored date string into strict YYYY-MM-DD for HTML5 <input type='date'>."""
    dt = _parse_any_date(val)
    return dt.strftime("%Y-%m-%d") if dt else ""


def fmt_date(val) -> str:
    """Clean human-readable date display (YYYY-MM-DD without '0:00:00')."""
    dt = _parse_any_date(val)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return str(val or "").strip()


templates.env.filters["to_iso_date"] = to_iso_date
templates.env.filters["fmt_date"] = fmt_date


def _calc_days(start_str: Optional[str], end_str: Optional[str]) -> Optional[int]:
    d1 = _parse_any_date(start_str)
    d2 = _parse_any_date(end_str)
    if d1 and d2:
        return max(0, (d2 - d1).days)
    return None


def _get_all_wells(conn):
    return [r["WellName"] for r in conn.execute('SELECT DISTINCT "WellName" FROM "tblwells" ORDER BY "WellName"').fetchall()]


def _get_well_run_dates(conn, well_name: str):
    d_set = []
    for tbl in ("tblwelldata", "tblwoverdata", "tblTBGdata", "tblpumpdata", "tblrodsdatanew", "tblwearcommentsdatanew"):
        sql = f'SELECT DISTINCT "WellRunDate" FROM "{tbl}" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0'
        for r in conn.execute(sql, (well_name,)).fetchall():
            if r[0] and r[0] not in d_set:
                d_set.append(r[0])
    return sorted(d_set, key=lambda x: _parse_any_date(x) or datetime(1900, 1, 1), reverse=True)


def _get_dropdowns(conn) -> dict:
    dd = {k: [] for k in DROPDOWN_FIELD_REGISTRY}
    for r in conn.execute('SELECT "field_key","option_value" FROM "app_dropdown_options" ORDER BY "field_key","sort_order","id"').fetchall():
        dd.setdefault(r["field_key"], []).append(r["option_value"])
    return dd


def _missing_well_redirect(path: str, wn: str, rd: str) -> RedirectResponse:
    missing = "Well Name and Run Date" if (not wn and not rd) else ("Well Name" if not wn else "Run Date")
    q = f"err={quote(f'Cannot save: Please select a {missing} first.')}"
    if wn:
        q += f"&well={quote(wn)}"
    return RedirectResponse(url=f"{path}?{q}", status_code=303)


def _ensure_well_run(conn, well_name: str, run_date: str):
    wn = (well_name or "").strip()
    rd = to_iso_date(run_date) or (run_date or "").strip()
    if not wn or not rd:
        raise HTTPException(status_code=400, detail="Well Name and Well Run Date are required.")
    both = f"{wn} {rd}".strip()
    if not conn.execute('SELECT 1 FROM "tblwells" WHERE "WellName"=?', (wn,)).fetchone():
        conn.execute('INSERT INTO "tblwells" ("WellName","notes") VALUES (?,?)', (wn, "Auto-added"))
    if not conn.execute('SELECT 1 FROM "tblwelldata" WHERE "WellName"=? AND "WellRunDate"=?', (wn, rd)).fetchone():
        conn.execute('INSERT INTO "tblwelldata" ("WellName","WellRunDate","Both") VALUES (?,?,?)', (wn, rd, both))
    return both


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all"):
    status = status if status in ("all", "finished", "progress") else "all"
    with db_session() as conn:
        stats = {
            "wells": conn.execute('SELECT COUNT(*) FROM "tblwells"').fetchone()[0],
            "runs": conn.execute('SELECT COUNT(*) FROM "tblwelldata"').fetchone()[0],
            "pumps": conn.execute('SELECT COUNT(*) FROM "tblpumpdata"').fetchone()[0],
            "tbg": conn.execute('SELECT COUNT(*) FROM "tblTBGdatanew"').fetchone()[0],
            "rods": conn.execute('SELECT COUNT(*) FROM "tblrodsdatanew"').fetchone()[0],
            "workovers": conn.execute('SELECT COUNT(*) FROM "tblwoverdata"').fetchone()[0],
            "wear": conn.execute('SELECT COUNT(*) FROM "tblwearcommentsdatanew"').fetchone()[0],
        }
        run_info = get_dashboard_runs(conn, status=status, limit=15)
    return templates.TemplateResponse("dashboard.html", {"request": request, "active": "dashboard", "stats": stats, "run_info": run_info, "db_info": get_db_status()})


@app.get("/wells", response_class=HTMLResponse)
def wells_page(request: Request, q: str = "", msg: str = ""):
    with db_session() as conn:
        if q.strip():
            like = f"%{q.strip()}%"
            wells = [dict(r) for r in conn.execute('SELECT * FROM "tblwells" WHERE "WellName" LIKE ? OR "notes" LIKE ? ORDER BY "WellName"', (like, like)).fetchall()]
            runs = [dict(r) for r in conn.execute('SELECT * FROM "tblwelldata" WHERE "WellName" LIKE ? OR "WellRunDate" LIKE ? ORDER BY "WellRunDate" DESC', (like, like)).fetchall()]
        else:
            wells = [dict(r) for r in conn.execute('SELECT * FROM "tblwells" ORDER BY "WellName"').fetchall()]
            runs = [dict(r) for r in conn.execute('SELECT * FROM "tblwelldata" ORDER BY "WellRunDate" DESC LIMIT 100').fetchall()]
    return templates.TemplateResponse("wells.html", {"request": request, "active": "wells", "wells": wells, "runs": runs, "q": q, "msg": msg})


@app.post("/wells/add")
def add_well(well_name: str = Form(...), notes: str = Form("")):
    wn = well_name.strip().upper()
    with db_session(write=True) as conn:
        if conn.execute('SELECT 1 FROM "tblwells" WHERE "WellName"=?', (wn,)).fetchone():
            conn.execute('UPDATE "tblwells" SET "notes"=? WHERE "WellName"=?', (notes.strip(), wn))
            log_audit(conn, "UPDATE", "tblwells", wn)
        else:
            conn.execute('INSERT INTO "tblwells" ("WellName","notes") VALUES (?,?)', (wn, notes.strip()))
            log_audit(conn, "INSERT", "tblwells", wn)
    return RedirectResponse(url=f"/wells?msg=Saved+well+{wn}", status_code=303)


@app.post("/wells/run/add")
def add_well_run(well_name: str = Form(...), run_date: str = Form(...)):
    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, well_name.strip().upper(), run_date.strip())
        log_audit(conn, "INSERT", "tblwelldata", both)
    return RedirectResponse(url=f"/wells?msg=Added+run+date+{both}", status_code=303)


@app.post("/wells/delete/{well_id}")
def delete_well(well_id: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblwells" WHERE "id"=?', (well_id,))
        log_audit(conn, "DELETE", "tblwells", f"id={well_id}")
    return RedirectResponse(url="/wells?msg=Well+deleted", status_code=303)


# STEP 1: WORKOVER & PULLED DATA (Auto-Filter Well + Historical Run Dates + Auto-Load)
@app.get("/workover", response_class=HTMLResponse)
def workover_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        current_wo = {}

        if selected_well:
            d_set = []
            for sql in (
                'SELECT DISTINCT "WellRunDate" FROM "tblwelldata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
                'SELECT DISTINCT "WellRunDate" FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
                'SELECT DISTINCT "WellRunDate" FROM "tblTBGdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
                'SELECT DISTINCT "WellRunDate" FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
            ):
                for r in conn.execute(sql, (selected_well,)).fetchall():
                    if r[0] and r[0] not in d_set:
                        d_set.append(r[0])
            run_dates = sorted(d_set, key=lambda x: _parse_any_date(x) or datetime(1900, 1, 1), reverse=True)
            if not selected_date and run_dates:
                selected_date = run_dates[0]

            if selected_date:
                row = conn.execute(
                    'SELECT * FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if row:
                    current_wo = dict(row)

        sql, params = 'SELECT * FROM "tblwoverdata" WHERE 1=1', []
        if selected_well:
            sql += ' AND "WellName" = ?'
            params.append(selected_well)
        workovers = [dict(r) for r in conn.execute(sql + ' ORDER BY "WOverDate" DESC, "_rowid_pk" DESC LIMIT 100', params).fetchall()]

    return templates.TemplateResponse(
        "workover.html",
        {
            "request": request, "active": "workover", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates,
            "selected_date": selected_date, "wo": current_wo,
            "workovers": workovers, "msg": msg,
        },
    )


@app.post("/workover/save")
def save_workover(
    rowid_pk: str = Form(""),
    WellName: str = Form(...), WellRunDate: str = Form(...), SDDate: str = Form(""), WOverDate: str = Form(""),
    RIGNAMErods: str = Form(""), TBlockWTrod: str = Form(""), rignametbg: str = Form(""), TBlockWTtbg: str = Form(""),
    Calrodstringwt: str = Form(""), Matched: str = Form("Yes"), CalOverPull: str = Form(""), ActOverPull: str = Form(""),
    PumpUnset: str = Form("Yes"), BackOff: str = Form("No"), Wearcomments: str = Form(""),
    Scalepresence: str = Form("No"), WellStatus: str = Form(""), WOreason: str = Form(""),
    FirstPTresult: str = Form(""), Pressuredroppedfrom: str = Form(""), Pressuredroppedto: str = Form(""),
    within_minutes: str = Form(""), Normreadingsfrom: str = Form(""), Normreadingsto: str = Form(""),
    Normstatus: str = Form(""), Pulledsandscreen: str = Form(""), PulledDHPtest: str = Form(""),
    MSVtest: str = Form("Yes"), MSVtestresult: str = Form(""), PSNringtest: str = Form(""),
    TACunsettrial: str = Form("No"), Wellfailurereason: str = Form(""), pumpfailure_type: str = Form(""),
    tbgfailuretype: str = Form(""), defectedtbgno: str = Form(""), tbgfailuredepth: str = Form(""),
    rodfailuretype: str = Form(""), Partedrodnofromsurface: str = Form(""), Partedrodsize: str = Form(""),
    Partedrodgrade: str = Form(""), Partdepth: str = Form(""),
    Wellspeed: str = Form(""), SL: str = Form(""), WC: str = Form(""), CL_PPM: str = Form(""),
    TempF: str = Form(""), H2S_PPM: str = Form(""), Rodrotatorstatus: str = Form(""),
    KTHslipsstatus: str = Form(""), polishedrodstatus: str = Form(""),
    Supervisor: str = Form(""), Foreman: str = Form(""), Generalnotes: str = Form(""),
):
    wn, rd = WellName.strip(), WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/workover", wn, rd)
    lifetime = _calc_days(rd, SDDate or WOverDate)
    def _int(v):
        try: return int(float(v)) if v and str(v).strip() else None
        except Exception: return None

    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, WellName, WellRunDate)
        existing_pk = int(rowid_pk) if rowid_pk and rowid_pk.strip().isdigit() else None
        if not existing_pk:
            ex = conn.execute('SELECT "_rowid_pk" FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate"=? LIMIT 1', (WellName.strip(), WellRunDate.strip())).fetchone()
            if ex:
                existing_pk = ex[0]

        vals = (
            WellName.strip(), WellRunDate.strip(), both, SDDate, WOverDate, lifetime,
            RIGNAMErods, TBlockWTrod, rignametbg, _int(TBlockWTtbg),
            _int(Calrodstringwt), Matched, _int(CalOverPull), _int(ActOverPull), PumpUnset, BackOff,
            Wearcomments, Scalepresence, WellStatus, WOreason, FirstPTresult,
            Pressuredroppedfrom, Pressuredroppedto, within_minutes,
            Normreadingsfrom, Normreadingsto, Normstatus, Pulledsandscreen, PulledDHPtest,
            MSVtest, MSVtestresult, PSNringtest, TACunsettrial,
            Wellfailurereason, pumpfailure_type, tbgfailuretype, defectedtbgno, tbgfailuredepth,
            rodfailuretype, Partedrodnofromsurface, Partedrodsize, Partedrodgrade, Partdepth,
            Wellspeed, SL, WC, CL_PPM, TempF, H2S_PPM,
            Rodrotatorstatus, KTHslipsstatus, polishedrodstatus, Supervisor, Foreman, Generalnotes,
        )

        if existing_pk:
            conn.execute(
                """UPDATE "tblwoverdata" SET
                    "WellName"=?, "WellRunDate"=?, "Both"=?, "SDDate"=?, "WOverDate"=?, "WellLifeTime"=?,
                    "RIGNAMErods"=?, "T/BlockWTrod"=?, "rignametbg"=?, "T/BlockWTtbg"=?,
                    "Calrodstringwt"=?, "Matched"=?, "CalOverPull"=?, "ActOverPull"=?, "PumpUnset"=?, "BackOff"=?,
                    "Wearcomments"=?, "Scalepresence"=?, "WellStatus"=?, "W/Oreason"=?, "1stP/Tresult"=?,
                    "Pressuredroppedfrom"=?, "Pressuredroppedto"=?, "within/minutes"=?,
                    "Normreadingsfrom"=?, "Normreadingsto"=?, "Normstatus"=?, "Pulledsandscreen"=?, "PulledDHPtest"=?,
                    "MSVtest"=?, "MSVtestresult"=?, "PSNringtest"=?, "TACunsettrial"=?,
                    "Wellfailurereason"=?, "pumpfailure type"=?, "tbgfailuretype"=?, "defectedtbgno"=?, "tbgfailuredepth"=?,
                    "rodfailuretype"=?, "Partedrodnofromsurface"=?, "Partedrodsize"=?, "Partedrodgrade"=?, "Partdepth"=?,
                    "Wellspeed"=?, "SL"=?, "W/C"=?, "CL(PPM)"=?, "Temprature(F0)"=?, "H2S(PPM)"=?,
                    "Rodrotatorstatus"=?, "KTHslipsstatus"=?, "polishedrodstatus"=?, "Supervisor"=?, "Foreman"=?, "Generalnotes"=?
                WHERE "_rowid_pk"=?""",
                vals + (existing_pk,),
            )
            log_audit(conn, "UPDATE", "tblwoverdata", both)
        else:
            conn.execute(
                """INSERT INTO "tblwoverdata" (
                    "WellName","WellRunDate","Both","SDDate","WOverDate","WellLifeTime",
                    "RIGNAMErods","T/BlockWTrod","rignametbg","T/BlockWTtbg",
                    "Calrodstringwt","Matched","CalOverPull","ActOverPull","PumpUnset","BackOff",
                    "Wearcomments","Scalepresence","WellStatus","W/Oreason","1stP/Tresult",
                    "Pressuredroppedfrom","Pressuredroppedto","within/minutes",
                    "Normreadingsfrom","Normreadingsto","Normstatus","Pulledsandscreen","PulledDHPtest",
                    "MSVtest","MSVtestresult","PSNringtest","TACunsettrial",
                    "Wellfailurereason","pumpfailure type","tbgfailuretype","defectedtbgno","tbgfailuredepth",
                    "rodfailuretype","Partedrodnofromsurface","Partedrodsize","Partedrodgrade","Partdepth",
                    "Wellspeed","SL","W/C","CL(PPM)","Temprature(F0)","H2S(PPM)",
                    "Rodrotatorstatus","KTHslipsstatus","polishedrodstatus","Supervisor","Foreman","Generalnotes"
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vals,
            )
            log_audit(conn, "INSERT", "tblwoverdata", both)
    return RedirectResponse(url=f"/workover?well={WellName.strip()}&run_date={WellRunDate.strip()}&msg=Workover+data+saved", status_code=303)


@app.post("/workover/delete/{pk}")
def delete_workover(pk: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblwoverdata" WHERE "_rowid_pk"=?', (pk,))
        log_audit(conn, "DELETE", "tblwoverdata", f"pk={pk}")
    return RedirectResponse(url="/workover?msg=Workover+deleted", status_code=303)


# STEP 2: PULLED ROD WEAR (Unified Form: Well+RunDate Loader, Multi-Section Intervals)
@app.get("/wear", response_class=HTMLResponse)
def wear_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        summary = None
        wear_meta = {"Pitting": "", "General Comments": ""}
        intervals = []
        loaded_from = ""

        if selected_well:
            run_dates = _get_well_run_dates(conn, selected_well)
            if not selected_date and run_dates:
                selected_date = run_dates[0]

            summary = calculate_well_summary(conn, selected_well, selected_date or None)

            if selected_date:
                wm = conn.execute(
                    'SELECT * FROM "tblwearcommentsdata2" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if wm:
                    wear_meta = dict(wm)

                saved_rows = [
                    dict(r)
                    for r in conn.execute(
                        'SELECT * FROM "tblwearcommentsdatanew" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY CAST("intervalno" AS INTEGER), "id"',
                        (selected_well, selected_date),
                    ).fetchall()
                ]
                if saved_rows:
                    loaded_from = "saved"
                    grouped = {}
                    for r in saved_rows:
                        inum = str(r.get("intervalno") or "1").strip()
                        grouped.setdefault(inum, []).append(r)
                    for inum, sec_list in grouped.items():
                        intervals.append({
                            "intervalno": inum,
                            "rod_type": sec_list[0].get("Rodtype") or "",
                            "rod_grade": sec_list[0].get("Rodgrade") or "",
                            "sections": sec_list,
                        })
                else:
                    rod_tapers = [
                        r for r in (summary.get("rod_rows") or [])
                        if not any(
                            skip in (str(r.get("itemtype") or "")).lower()
                            for skip in ("pony", "polished", "sand screen", "dip tube", "stab")
                        )
                    ]
                    if rod_tapers:
                        loaded_from = "rod_string"
                        start_depth = round(float(summary.get("orkb") or 22) + float(summary.get("pony_length") or 0) + 23.0)
                        cur_depth = start_depth
                        for idx, rt in enumerate(rod_tapers, 1):
                            cnt = int(float(rt.get("itemno") or 0))
                            length = float(rt.get("itemlength") or (cnt * 25))
                            end_depth = round(cur_depth + length)
                            intervals.append({
                                "intervalno": str(idx),
                                "rod_type": rt.get("itemtype") or '1" Normal',
                                "rod_grade": rt.get("itemsize") or "N-97",
                                "sections": [{
                                    "intervalno": str(idx),
                                    "Rodtype": rt.get("itemtype") or '1" Normal',
                                    "Rodgrade": rt.get("itemsize") or "N-97",
                                    "Noofrods": str(cnt) if cnt else "",
                                    "FRod": "1",
                                    "TRod": str(cnt) if cnt else "",
                                    "FDepth": str(cur_depth),
                                    "TDepth": str(end_depth),
                                    "Coupling": "-", "Shoulder": "-", "Neck": "-",
                                    "Body": "-", "Guides": "-", "Wearrate": "-",
                                }],
                            })
                            cur_depth = end_depth

        if selected_well and not intervals:
            intervals.append({
                "intervalno": "1",
                "rod_type": '1" Normal',
                "rod_grade": "N-97",
                "sections": [{
                    "intervalno": "1", "Rodtype": '1" Normal', "Rodgrade": "N-97",
                    "Noofrods": "", "FRod": "1", "TRod": "", "FDepth": "", "TDepth": "",
                    "Coupling": "-", "Shoulder": "-", "Neck": "-",
                    "Body": "-", "Guides": "-", "Wearrate": "-",
                }],
            })

        hsql, hparams = 'SELECT * FROM "tblwearcommentsdatanew" WHERE 1=1', []
        if selected_well:
            hsql += ' AND "WellName" = ?'
            hparams.append(selected_well)
        rows = [dict(r) for r in conn.execute(hsql + ' ORDER BY "WellName", "WellRunDate" DESC, CAST("intervalno" AS INTEGER), "id" LIMIT 200', hparams).fetchall()]

    base_depth_offset = round(float(summary.get("orkb") or 22) + float(summary.get("pony_length") or 0) + 23.0) if summary else 59

    return templates.TemplateResponse(
        "wear.html",
        {
            "request": request, "active": "wear", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates, "selected_date": selected_date,
            "summary": summary, "intervals": intervals, "wear_meta": wear_meta,
            "loaded_from": loaded_from, "base_depth_offset": base_depth_offset,
            "rows": rows, "msg": msg,
        },
    )


@app.post("/wear/save")
def save_wear_sheet(
    WellName: str = Form(...),
    WellRunDate: str = Form(...),
    Pitting: str = Form(""),
    GeneralComments: str = Form(""),
    intervalno: List[str] = Form(default=[]),
    Rodtype: List[str] = Form(default=[]),
    Rodgrade: List[str] = Form(default=[]),
    Noofrods: List[str] = Form(default=[]),
    FRod: List[str] = Form(default=[]),
    TRod: List[str] = Form(default=[]),
    FDepth: List[str] = Form(default=[]),
    TDepth: List[str] = Form(default=[]),
    Coupling: List[str] = Form(default=[]),
    Shoulder: List[str] = Form(default=[]),
    Neck: List[str] = Form(default=[]),
    Body: List[str] = Form(default=[]),
    Guides: List[str] = Form(default=[]),
    Wearrate: List[str] = Form(default=[]),
):
    wn = WellName.strip()
    rd = WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/wear", wn, rd)
    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, wn, rd)
        conn.execute('DELETE FROM "tblwearcommentsdatanew" WHERE "WellName"=? AND "WellRunDate"=?', (wn, rd))
        n_rows = len(intervalno)
        saved_cnt = 0
        for i in range(n_rows):
            rt = Rodtype[i].strip() if i < len(Rodtype) else ""
            no_r = Noofrods[i].strip() if i < len(Noofrods) else ""
            fr = FRod[i].strip() if i < len(FRod) else ""
            tr = TRod[i].strip() if i < len(TRod) else ""
            if not (rt or no_r or fr or tr):
                continue
            conn.execute(
                """INSERT INTO "tblwearcommentsdatanew" (
                    "WellName","WellRunDate","intervalno","Rodtype","Rodgrade","Noofrods",
                    "FRod","TRod","FDepth","TDepth","Coupling","Shoulder","Neck","Body","Guides","Wearrate","Both"
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    wn, rd,
                    intervalno[i].strip() if i < len(intervalno) else "1",
                    rt,
                    Rodgrade[i].strip() if i < len(Rodgrade) else "",
                    no_r, fr, tr,
                    FDepth[i].strip() if i < len(FDepth) else "",
                    TDepth[i].strip() if i < len(TDepth) else "",
                    Coupling[i].strip() if i < len(Coupling) else "-",
                    Shoulder[i].strip() if i < len(Shoulder) else "-",
                    Neck[i].strip() if i < len(Neck) else "-",
                    Body[i].strip() if i < len(Body) else "-",
                    Guides[i].strip() if i < len(Guides) else "-",
                    Wearrate[i].strip() if i < len(Wearrate) else "-",
                    both,
                ),
            )
            saved_cnt += 1

        exists = conn.execute(
            'SELECT "_rowid_pk" FROM "tblwearcommentsdata2" WHERE "WellName"=? AND "WellRunDate"=?',
            (wn, rd),
        ).fetchone()
        if exists:
            conn.execute(
                'UPDATE "tblwearcommentsdata2" SET "Pitting"=?, "General Comments"=? WHERE "_rowid_pk"=?',
                (Pitting, GeneralComments, exists[0]),
            )
        else:
            conn.execute(
                'INSERT INTO "tblwearcommentsdata2" ("WellName","WellRunDate","Both","Pitting","General Comments") VALUES (?,?,?,?,?)',
                (wn, rd, both, Pitting, GeneralComments),
            )
        log_audit(conn, "SAVE_WEAR", "tblwearcommentsdatanew", f"{both} ({saved_cnt} sections)")

    return RedirectResponse(url=f"/wear?well={wn}&run_date={rd}&msg=Saved+wear+sheet+({saved_cnt}+sections)", status_code=303)


@app.post("/wear/delete/{wear_id}")
def delete_wear(wear_id: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblwearcommentsdatanew" WHERE "id"=?', (wear_id,))
        log_audit(conn, "DELETE", "tblwearcommentsdatanew", f"id={wear_id}")
    return RedirectResponse(url="/wear?msg=Wear+record+deleted", status_code=303)


# STEP 3: NEW TUBING STRING (TBG) — Unified Well+RunDate Auto-Loader + Dynamic Tally
@app.get("/tbg", response_class=HTMLResponse)
def tbg_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        tbg_master = {}
        tbg_items = []

        if selected_well:
            run_dates = _get_well_run_dates(conn, selected_well)
            if not selected_date and run_dates:
                selected_date = run_dates[0]
            if selected_date:
                m_row = conn.execute(
                    'SELECT * FROM "tblTBGdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if m_row:
                    tbg_master = dict(m_row)
                tbg_items = [
                    dict(r)
                    for r in conn.execute(
                        'SELECT * FROM "tblTBGdatanew" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY CAST("intervalno" AS INTEGER), "id"',
                        (selected_well, selected_date),
                    ).fetchall()
                ]

        if selected_well and selected_date and not tbg_items:
            tbg_items = [{
                "intervalno": "1", "itemtype": "TBG Jnts", "itemno": "1",
                "itemlength": "30.9", "itemsize": "3 1/2", "itemdescrip": "3 1/2 EUE (FLAT)", "itemcond": "A",
            }]

        hsql, hparams = 'SELECT * FROM "tblTBGdatanew" WHERE 1=1', []
        if selected_well:
            hsql += ' AND "WellName" = ?'
            hparams.append(selected_well)
        history_items = [
            dict(r)
            for r in conn.execute(
                hsql + ' ORDER BY "WellName", "WellRunDate" DESC, CAST("intervalno" AS INTEGER), "id" LIMIT 300',
                hparams,
            ).fetchall()
        ]

    return templates.TemplateResponse(
        "tbg.html",
        {
            "request": request, "active": "tbg", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates, "selected_date": selected_date,
            "tm": tbg_master, "tbg_items": tbg_items, "history_items": history_items, "msg": msg,
        },
    )


@app.post("/tbg/save")
def save_tbg_unified(
    WellName: str = Form(...), WellRunDate: str = Form(...),
    CompletionType: str = Form("WLEG"), ORKB: str = Form("22"),
    RIHtbgrig: str = Form(""), tbgrihrigblock: str = Form(""),
    TACDepth: str = Form(""), SNDepth: str = Form(""),
    TopPerfDepth: str = Form(""), BotPerfDepth: str = Form(""), PBTD: str = Form(""),
    WLEGDepth: str = Form(""), BULLPLUGDepth: str = Form(""),
    NIPPLEdepth: str = Form(""), HallPHLHYDPKRDepth: str = Form(""),
    KTHFlangeSize: str = Form(""), KTHFlangeType: str = Form(""),
    X_under_collar: str = Form(""), TBGStretch: str = Form(""), TACOverPull: str = Form(""),
    WeightStart: str = Form(""), WeightEnd: str = Form(""),
    TBGStartDate: str = Form(""), TBGFailureDate: str = Form(""),
    Generalcomments: str = Form(""),
    intervalno: List[str] = Form(default=[]),
    itemtype: List[str] = Form(default=[]),
    itemno: List[str] = Form(default=[]),
    itemlength: List[str] = Form(default=[]),
    itemsize: List[str] = Form(default=[]),
    itemdescrip: List[str] = Form(default=[]),
    itemcond: List[str] = Form(default=[]),
):
    wn, rd = WellName.strip(), WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/tbg", wn, rd)
    tbg_lifetime = _calc_days(TBGStartDate, TBGFailureDate)
    try:
        orkb_val = int(float(ORKB)) if ORKB and str(ORKB).strip() else None
    except Exception:
        orkb_val = None
    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, wn, rd)
        exists = conn.execute(
            'SELECT "_rowid_pk" FROM "tblTBGdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
            (wn, rd),
        ).fetchone()
        vals = (
            CompletionType, orkb_val, RIHtbgrig, tbgrihrigblock, TACDepth, SNDepth, TopPerfDepth,
            BotPerfDepth, PBTD, WLEGDepth, BULLPLUGDepth, NIPPLEdepth, HallPHLHYDPKRDepth,
            KTHFlangeSize, KTHFlangeType, X_under_collar, TBGStretch, TACOverPull,
            WeightStart, WeightEnd, TBGStartDate, TBGFailureDate, tbg_lifetime, Generalcomments,
        )
        if exists:
            conn.execute(
                """UPDATE "tblTBGdata" SET "CompletionType"=?, "ORKB"=?, "RIHtbgrig"=?, "tbgrihrigblock"=?,
                   "TACDepth"=?, "SNDepth"=?, "TopPerfDepth"=?, "BotPerfDepth"=?, "PBTD"=?,
                   "WLEGDepth"=?, "BULLPLUGDepth"=?, "NIPPLEdepth"=?, "HallPHLHYDPKRDepth"=?,
                   "KTHFlangeSize"=?, "KTHFlangeType"=?, "Distanceundercollar(X)"=?, "TBGStretch"=?,
                   "TACOverPull"=?, "WeightIndicator(Start)"=?, "WeightIndicator(End)"=?,
                   "TBGStartDate"=?, "TBGFailureDate"=?, "TBGLifeTime"=?, "Generalcomments"=? WHERE "_rowid_pk"=?""",
                vals + (exists[0],),
            )
        else:
            conn.execute(
                """INSERT INTO "tblTBGdata" (
                    "WellName","WellRunDate","Both","CompletionType","ORKB","RIHtbgrig","tbgrihrigblock",
                    "TACDepth","SNDepth","TopPerfDepth","BotPerfDepth","PBTD",
                    "WLEGDepth","BULLPLUGDepth","NIPPLEdepth","HallPHLHYDPKRDepth",
                    "KTHFlangeSize","KTHFlangeType","Distanceundercollar(X)","TBGStretch","TACOverPull",
                    "WeightIndicator(Start)","WeightIndicator(End)","TBGStartDate","TBGFailureDate","TBGLifeTime","Generalcomments"
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wn, rd, both) + vals,
            )

        conn.execute('DELETE FROM "tblTBGdatanew" WHERE "WellName"=? AND "WellRunDate"=?', (wn, rd))
        saved_cnt = 0
        for i in range(len(itemtype)):
            it = itemtype[i].strip() if i < len(itemtype) else ""
            no_v = itemno[i].strip() if i < len(itemno) else ""
            len_v = itemlength[i].strip() if i < len(itemlength) else ""
            sz_v = itemsize[i].strip() if i < len(itemsize) else ""
            desc_v = itemdescrip[i].strip() if i < len(itemdescrip) else ""
            cond_v = itemcond[i].strip() if i < len(itemcond) else ""
            ord_v = intervalno[i].strip() if i < len(intervalno) and intervalno[i].strip() else str(saved_cnt + 1)
            if not (it or no_v or len_v or desc_v):
                continue
            saved_cnt += 1
            conn.execute(
                'INSERT INTO "tblTBGdatanew" ("WellName","WellRunDate","itemtype","itemno","itemlength","itemsize","itemdescrip","itemcond","completiontype","intervalno","Both") VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (wn, rd, it, no_v, len_v, sz_v, desc_v, cond_v, CompletionType, ord_v, both),
            )
        log_audit(conn, "SAVE_TBG", "tblTBGdata", f"{both} ({saved_cnt} items)")

    return RedirectResponse(url=f"/tbg?well={wn}&run_date={rd}&msg=Saved+tubing+string+({saved_cnt}+items)", status_code=303)


@app.post("/tbg/item/delete/{item_id}")
def delete_tbg_item(item_id: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblTBGdatanew" WHERE "id"=?', (item_id,))
        log_audit(conn, "DELETE", "tblTBGdatanew", f"id={item_id}")
    return RedirectResponse(url="/tbg?msg=Tubing+item+deleted", status_code=303)


# STEP 4: DOWNHOLE PUMP DATA
@app.get("/pumps", response_class=HTMLResponse)
def pumps_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        current_pump = {}

        if selected_well:
            run_dates = _get_well_run_dates(conn, selected_well)
            if not selected_date and run_dates:
                selected_date = run_dates[0]
            if selected_date:
                row = conn.execute(
                    'SELECT * FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if row:
                    current_pump = dict(row)

        sql, params = 'SELECT * FROM "tblpumpdata" WHERE 1=1', []
        if selected_well:
            sql += ' AND "WellName" = ?'
            params.append(selected_well)
        pumps = [dict(r) for r in conn.execute(sql + ' ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC LIMIT 200', params).fetchall()]

    return templates.TemplateResponse(
        "pumps.html",
        {
            "request": request, "active": "pumps", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates,
            "selected_date": selected_date, "pump": current_pump,
            "pumps": pumps, "msg": msg,
        },
    )


@app.post("/pumps/save")
def save_pump_data(
    rowid_pk: str = Form(""),
    WellName: str = Form(...), WellRunDate: str = Form(...),
    Pumpsize: str = Form("2.25"), PumpType: str = Form("Bottom"),
    Extension: str = Form("2,2"), BarrelLength: str = Form("20"),
    Vendor: str = Form("HF"), PlugSeat: str = Form("Normal (A)"),
    PlungerClearance: str = Form("-0.005"), PumpSN: str = Form(""),
    PumpCOND: str = Form("A"), PumpLength: str = Form("26"),
    Pumpdesignation: str = Form(""),
    SV: str = Form("SS (A)"), DSV: str = Form("SS (A)"),
    TV: str = Form("Monel (A)"), DTV: str = Form("SS (A)"),
    Openplunger: str = Form("Monel (A)"),
    StartDate: str = Form(""), FailureDate: str = Form(""),
    notes: str = Form(""),
):
    wn = WellName.strip()
    rd = to_iso_date(WellRunDate) or WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/pumps", wn, rd)
    StartDate = to_iso_date(StartDate) or StartDate.strip()
    FailureDate = to_iso_date(FailureDate) or FailureDate.strip()
    lifetime = _calc_days(StartDate, FailureDate)
    if not Pumpdesignation.strip():
        code = "RHBM" if PumpType.strip().lower().startswith("b") else "RHAM"
        sz_code = Pumpsize.strip().replace(".", "")
        ext_code = Extension.strip().replace(" ", "").replace(",", "-")
        Pumpdesignation = f"30-{sz_code}-{code}-{BarrelLength.strip()}-6-{ext_code}"

    def _num(v, cast=float):
        try:
            return cast(float(v)) if v and str(v).strip() else None
        except Exception:
            return None

    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, wn, rd)
        existing_pk = int(rowid_pk) if rowid_pk and rowid_pk.strip().isdigit() else None
        if not existing_pk:
            ex = conn.execute(
                'SELECT "_rowid_pk" FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                (wn, rd),
            ).fetchone()
            if ex:
                existing_pk = ex[0]

        vals = (
            wn, rd, both, Pumpsize, PumpType, Extension, _num(BarrelLength, float),
            Vendor, PlugSeat, PlungerClearance, PumpSN, PumpCOND,
            _num(PumpLength, int), Pumpdesignation,
            SV, DSV, TV, DTV, Openplunger, StartDate, FailureDate, lifetime, notes,
        )

        if existing_pk:
            conn.execute(
                """UPDATE "tblpumpdata" SET
                    "WellName"=?, "WellRunDate"=?, "Both"=?, "Pumpsize"=?, "PumpType"=?, "Extension"=?, "Field1"=?,
                    "Vendor"=?, "PlugSeat"=?, "PlungerClearance"=?, "PumpSN"=?, "PumpCOND"=?,
                    "PumpLength"=?, "Pumpdesignation"=?,
                    "SV"=?, "DSV"=?, "TV"=?, "DTV"=?, "Openplunger"=?,
                    "StartDate"=?, "FailureDate"=?, "PumpLifetime"=?, "notes"=?
                WHERE "_rowid_pk"=?""",
                vals + (existing_pk,),
            )
            log_audit(conn, "UPDATE", "tblpumpdata", f"PumpData: {both}")
        else:
            conn.execute(
                """INSERT INTO "tblpumpdata" (
                    "WellName","WellRunDate","Both","Pumpsize","PumpType","Extension","Field1",
                    "Vendor","PlugSeat","PlungerClearance","PumpSN","PumpCOND",
                    "PumpLength","Pumpdesignation",
                    "SV","DSV","TV","DTV","Openplunger","StartDate","FailureDate","PumpLifetime","notes"
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vals,
            )
            log_audit(conn, "INSERT", "tblpumpdata", f"PumpData: {both}")

    return RedirectResponse(url=f"/pumps?well={wn}&run_date={rd}&msg=Downhole+pump+data+saved", status_code=303)


@app.post("/pumps/delete/{pk}")
def delete_pump(pk: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblpumpdata" WHERE "_rowid_pk"=?', (pk,))
        log_audit(conn, "DELETE", "tblpumpdata", f"pk={pk}")
    return RedirectResponse(url="/pumps?msg=Pump+record+deleted", status_code=303)


# STEP 6: WORKSHOP PUMP TEAR DOWN & INSPECTION
@app.get("/pumps/teardown", response_class=HTMLResponse)
def pump_teardown_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        pump = {}
        workover = {}
        tbg_master = {}

        if selected_well:
            run_dates = _get_well_run_dates(conn, selected_well)
            if not selected_date and run_dates:
                selected_date = run_dates[0]
            if selected_date:
                p_row = conn.execute(
                    'SELECT * FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if p_row:
                    pump = dict(p_row)
                w_row = conn.execute(
                    'SELECT * FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if w_row:
                    workover = dict(w_row)
                t_row = conn.execute(
                    'SELECT * FROM "tblTBGdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if t_row:
                    tbg_master = dict(t_row)

        sql, params = 'SELECT * FROM "tblpumpdata" WHERE 1=1', []
        if selected_well:
            sql += ' AND "WellName" = ?'
            params.append(selected_well)
        inspections = [dict(r) for r in conn.execute(sql + ' ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC LIMIT 100', params).fetchall()]

    return templates.TemplateResponse(
        "pump_teardown.html",
        {
            "request": request, "active": "pump_teardown", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates, "selected_date": selected_date,
            "pump": pump, "workover": workover, "tbg_master": tbg_master,
            "inspections": inspections, "msg": msg,
        },
    )


@app.post("/pumps/teardown/save")
def save_pump_teardown(
    rowid_pk: str = Form(""),
    WellName: str = Form(...), WellRunDate: str = Form(...),
    pumpreportdate: str = Form(""), welltype: str = Form("Vertical"), pumpinspectioncond: str = Form(""),
    barrelcond: str = Form(""), barreldefect: str = Form(""), barrelapparentfailure: str = Form(""),
    rodguidecond: str = Form(""), rodguidedefect: str = Form(""), rodguideapparentfailure: str = Form(""),
    barrelconnectorcond: str = Form(""), barrelconnectordefect: str = Form(""), barrelconnectorapparentfailure: str = Form(""),
    topextcond: str = Form(""), topextdefect: str = Form(""), topextapparentfailure: str = Form(""),
    botextcond: str = Form(""), botextdefect: str = Form(""), botextapparentfailure: str = Form(""),
    SVcond: str = Form(""), SVdefect: str = Form(""), SVapparentfailure: str = Form(""),
    SVballcond: str = Form(""), SVseatcond: str = Form(""), SVballseattest: str = Form(""), SVballseatcomment: str = Form(""),
    DSVcond: str = Form(""), DSVdefect: str = Form(""), DSVapparentfailure: str = Form(""),
    DSVballcond: str = Form(""), DSVseatcond: str = Form(""), DSVballseattest: str = Form(""), DSVballseatcomment: str = Form(""),
    coup_pseatcond: str = Form(""), coup_pseatdefect: str = Form(""), coup_pseatapparentfailure: str = Form(""),
    plungercond: str = Form(""), plungerdefect: str = Form(""), plungerapparentfailure: str = Form(""),
    vrodcond: str = Form(""), vroddefect: str = Form(""), vrodapparentfailure: str = Form(""),
    vrodbushingcond: str = Form(""), vrodbushingdefect: str = Form(""), vrodbushingapparentfailure: str = Form(""),
    vrodnutcond: str = Form(""), vrodnutdefect: str = Form(""), vrodnutapparentfailure: str = Form(""),
    oplungercond: str = Form(""), oplungerdefect: str = Form(""), oplungerapparentfailure: str = Form(""),
    TVcond: str = Form(""), TVdefect: str = Form(""), TVapparentfailure: str = Form(""),
    TVballcond: str = Form(""), TVseatcond: str = Form(""), TVballseattest: str = Form(""), TVballseatcomment: str = Form(""),
    DTVcond: str = Form(""), DTVdefect: str = Form(""), DTVapparentfailure: str = Form(""),
    DTVballcond: str = Form(""), DTVseatcond: str = Form(""), DTVballseattest: str = Form(""), DTVballseatcomment: str = Form(""),
    TVpseatcond: str = Form(""), TVpseatdefect: str = Form(""), TVpseatapparentfailure: str = Form(""),
    hdownbushcond: str = Form(""), hdownbushdefect: str = Form(""), hdownbushapparentfailure: str = Form(""),
    mandrelcond: str = Form(""), mandreldefect: str = Form(""), mandrelapparentfailure: str = Form(""),
    sleevecond: str = Form(""), sleevedefect: str = Form(""), sleeveapparentfailure: str = Form(""),
    hdownringcond: str = Form(""), hdownringdefect: str = Form(""), hdownringapparentfailure: str = Form(""),
    comments: str = Form(""), locationonpc: str = Form(""),
    preparedby: str = Form(""), inspectby: str = Form(""),
):
    wn, rd = WellName.strip(), WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/pumps/teardown", wn, rd)
    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, wn, rd)
        existing_pk = int(rowid_pk) if rowid_pk and rowid_pk.strip().isdigit() else None
        if not existing_pk:
            ex = conn.execute(
                'SELECT "_rowid_pk" FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                (wn, rd),
            ).fetchone()
            if ex:
                existing_pk = ex[0]

        td_cols = [
            "pumpreportdate", "welltype", "pumpinspectioncond",
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
            "comments", "locationonpc", "preparedby", "inspectby",
        ]
        td_vals = [
            pumpreportdate, welltype, pumpinspectioncond,
            barrelcond, barreldefect, barrelapparentfailure,
            rodguidecond, rodguidedefect, rodguideapparentfailure,
            barrelconnectorcond, barrelconnectordefect, barrelconnectorapparentfailure,
            topextcond, topextdefect, topextapparentfailure,
            botextcond, botextdefect, botextapparentfailure,
            SVcond, SVdefect, SVapparentfailure,
            SVballcond, SVseatcond, SVballseattest, SVballseatcomment,
            DSVcond, DSVdefect, DSVapparentfailure,
            DSVballcond, DSVseatcond, DSVballseattest, DSVballseatcomment,
            coup_pseatcond, coup_pseatdefect, coup_pseatapparentfailure,
            plungercond, plungerdefect, plungerapparentfailure,
            vrodcond, vroddefect, vrodapparentfailure,
            vrodbushingcond, vrodbushingdefect, vrodbushingapparentfailure,
            vrodnutcond, vrodnutdefect, vrodnutapparentfailure,
            oplungercond, oplungerdefect, oplungerapparentfailure,
            TVcond, TVdefect, TVapparentfailure,
            TVballcond, TVseatcond, TVballseattest, TVballseatcomment,
            DTVcond, DTVdefect, DTVapparentfailure,
            DTVballcond, DTVseatcond, DTVballseattest, DTVballseatcomment,
            TVpseatcond, TVpseatdefect, TVpseatapparentfailure,
            hdownbushcond, hdownbushdefect, hdownbushapparentfailure,
            mandrelcond, mandreldefect, mandrelapparentfailure,
            sleevecond, sleevedefect, sleeveapparentfailure,
            hdownringcond, hdownringdefect, hdownringapparentfailure,
            comments, locationonpc, preparedby, inspectby,
        ]

        if existing_pk:
            set_clause = ", ".join(f'"{c}"=?' for c in td_cols)
            conn.execute(f'UPDATE "tblpumpdata" SET {set_clause} WHERE "_rowid_pk"=?', td_vals + [existing_pk])
            log_audit(conn, "UPDATE", "tblpumpdata", f"TearDown: {both}")
        else:
            all_cols = ["WellName", "WellRunDate", "Both"] + td_cols
            col_sql = ", ".join(f'"{c}"' for c in all_cols)
            ph_sql = ", ".join("?" for _ in all_cols)
            conn.execute(f'INSERT INTO "tblpumpdata" ({col_sql}) VALUES ({ph_sql})', [wn, rd, both] + td_vals)
            log_audit(conn, "INSERT", "tblpumpdata", f"TearDown: {both}")

    return RedirectResponse(url=f"/pumps/teardown?well={wn}&run_date={rd}&msg=Workshop+teardown+saved", status_code=303)


# STEP 5: NEW ROD STRING (RIH)
@app.get("/rods", response_class=HTMLResponse)
def rods_page(request: Request, well: str = "", run_date: str = "", msg: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        dd = _get_dropdowns(conn)
        selected_well = well.strip()
        run_dates = []
        selected_date = run_date.strip()
        rod_meta = {}
        rod_items = []

        if selected_well:
            run_dates = _get_well_run_dates(conn, selected_well)
            if not selected_date and run_dates:
                selected_date = run_dates[0]
            if selected_date:
                m_row = conn.execute(
                    'SELECT * FROM "tblrodstringdata2" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
                    (selected_well, selected_date),
                ).fetchone()
                if m_row:
                    rod_meta = dict(m_row)
                rod_items = [
                    dict(r)
                    for r in conn.execute(
                        'SELECT * FROM "tblrodsdatanew" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY CAST("intervalno" AS INTEGER), "id"',
                        (selected_well, selected_date),
                    ).fetchall()
                ]

        intake_items = []
        if selected_well and selected_date:
            intake_items = [dict(r) for r in conn.execute(
                'SELECT * FROM "app_pump_intake_items" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY CAST("intervalno" AS INTEGER), "id"',
                (selected_well, selected_date)).fetchall()]

        if selected_well and selected_date and not rod_items:
            rod_items = [{
                "intervalno": "1", "itemtype": '1" Normal', "itemno": "1",
                "itemlength": 25.0, "itemsize": "N-97", "itemcond": "A",
            }]

        hsql, hparams = 'SELECT * FROM "tblrodsdatanew" WHERE 1=1', []
        if selected_well:
            hsql += ' AND "WellName" = ?'
            hparams.append(selected_well)
        history_rods = [
            dict(r)
            for r in conn.execute(
                hsql + ' ORDER BY "WellName", "WellRunDate" DESC, CAST("intervalno" AS INTEGER), "id" LIMIT 300',
                hparams,
            ).fetchall()
        ]

    return templates.TemplateResponse(
        "rods.html",
        {
            "request": request, "active": "rods", "wells": wells, "dd": dd,
            "selected_well": selected_well, "run_dates": run_dates, "selected_date": selected_date,
            "rm": rod_meta, "rod_items": rod_items, "intake_items": intake_items, "history_rods": history_rods, "msg": msg,
        },
    )


@app.post("/rods/save")
def save_rods_unified(
    WellName: str = Form(...), WellRunDate: str = Form(...),
    RigName: str = Form(""), rodrihrigblock: str = Form(""),
    JobType: str = Form("P/R"), RodRotator: str = Form("NO"),
    Changed1: str = Form(""), Changed2: str = Form(""),
    Supervisor: str = Form(""), Foreman: str = Form(""),
    Comments: str = Form(""),
    intervalno: List[str] = Form(default=[]),
    itemtype: List[str] = Form(default=[]),
    itemno: List[str] = Form(default=[]),
    itemlength: List[str] = Form(default=[]),
    itemsize: List[str] = Form(default=[]),
    itemcond: List[str] = Form(default=[]),
    intake_type: List[str] = Form(default=[]),
    intake_no: List[str] = Form(default=[]),
    intake_length: List[str] = Form(default=[]),
    intake_size: List[str] = Form(default=[]),
    intake_cond: List[str] = Form(default=[]),
):
    wn, rd = WellName.strip(), WellRunDate.strip()
    if not wn or not rd:
        return _missing_well_redirect("/rods", wn, rd)

    def _flt(v):
        try:
            return float(v) if v and str(v).strip() else 0.0
        except Exception:
            return 0.0

    with db_session(write=True) as conn:
        both = _ensure_well_run(conn, wn, rd)
        exists = conn.execute(
            'SELECT "_rowid_pk" FROM "tblrodstringdata2" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
            (wn, rd),
        ).fetchone()
        if exists:
            conn.execute(
                'UPDATE "tblrodstringdata2" SET "RigName"=?, "rodrihrigblock"=?, "JobType"=?, "RodRotator"=?, "Changed1"=?, "Changed2"=?, "Supervisor"=?, "Foreman"=?, "Comments/Notes"=? WHERE "_rowid_pk"=?',
                (RigName, rodrihrigblock, JobType, RodRotator, Changed1, Changed2, Supervisor, Foreman, Comments, exists[0]),
            )
        else:
            conn.execute(
                'INSERT INTO "tblrodstringdata2" ("WellName","WellRunDate","Both","RigName","rodrihrigblock","JobType","RodRotator","Changed1","Changed2","Supervisor","Foreman","Comments/Notes") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (wn, rd, both, RigName, rodrihrigblock, JobType, RodRotator, Changed1, Changed2, Supervisor, Foreman, Comments),
            )

        conn.execute('DELETE FROM "tblrodsdatanew" WHERE "WellName"=? AND "WellRunDate"=?', (wn, rd))
        saved_cnt = 0
        for i in range(len(itemtype)):
            it = itemtype[i].strip() if i < len(itemtype) else ""
            no_v = itemno[i].strip() if i < len(itemno) else ""
            len_v = _flt(itemlength[i] if i < len(itemlength) else "")
            sz_v = itemsize[i].strip() if i < len(itemsize) else ""
            cond_v = itemcond[i].strip() if i < len(itemcond) else ""
            ord_v = intervalno[i].strip() if i < len(intervalno) and intervalno[i].strip() else str(saved_cnt + 1)
            if not (it or no_v or len_v or sz_v):
                continue
            saved_cnt += 1
            conn.execute(
                'INSERT INTO "tblrodsdatanew" ("WellName","WellRunDate","intervalno","itemtype","itemno","itemlength","itemsize","itemcond","Both") VALUES (?,?,?,?,?,?,?,?,?)',
                (wn, rd, ord_v, it, no_v, len_v, sz_v, cond_v, both),
            )
        conn.execute('DELETE FROM "app_pump_intake_items" WHERE "WellName"=? AND "WellRunDate"=?', (wn, rd))
        intake_cnt = 0
        for i in range(len(intake_type)):
            it = intake_type[i].strip()
            no_v = intake_no[i].strip() if i < len(intake_no) else ""
            len_v = _flt(intake_length[i] if i < len(intake_length) else "")
            sz_v = intake_size[i].strip() if i < len(intake_size) else ""
            cond_v = intake_cond[i].strip() if i < len(intake_cond) else ""
            if not (it or no_v or len_v or sz_v):
                continue
            intake_cnt += 1
            conn.execute(
                'INSERT INTO "app_pump_intake_items" ("WellName","WellRunDate","intervalno","itemtype","itemno","itemlength","itemsize","itemcond","Both") VALUES (?,?,?,?,?,?,?,?,?)',
                (wn, rd, str(intake_cnt), it, no_v, len_v, sz_v, cond_v, both),
            )
        log_audit(conn, "SAVE_RODS", "tblrodsdatanew", f"{both} ({saved_cnt} sections, {intake_cnt} intake)")

    return RedirectResponse(url=f"/rods?well={wn}&run_date={rd}&msg=Saved+rod+string+({saved_cnt}+items,+{intake_cnt}+below+pump)", status_code=303)


@app.post("/rods/delete/{rod_id}")
def delete_rod(rod_id: int):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "tblrodsdatanew" WHERE "id"=?', (rod_id,))
        log_audit(conn, "DELETE", "tblrodsdatanew", f"id={rod_id}")
    return RedirectResponse(url="/rods?msg=Rod+section+deleted", status_code=303)


# =============================================================================
# ADMIN PANEL: DYNAMIC DROPDOWN LIST MANAGER + ACCESS (.ACCDB) IMPORTER
# =============================================================================
def _render_admin_page(
    request: Request,
    field_key: str = "wo_rig_name",
    msg: str = "",
    error_msg: str = "",
    restore_error_msg: str = "",
    import_report: Optional[dict] = None,
    restore_report: Optional[dict] = None,
):
    with db_session() as conn:
        options = [dict(r) for r in conn.execute('SELECT * FROM "app_dropdown_options" WHERE "field_key"=? ORDER BY "sort_order","id"', (field_key,)).fetchall()]
        counts_by_field = {r["field_key"]: r["cnt"] for r in conn.execute('SELECT "field_key", COUNT(*) AS cnt FROM "app_dropdown_options" GROUP BY "field_key"').fetchall()}
        recent_audit = [dict(r) for r in conn.execute('SELECT * FROM "app_audit_log" ORDER BY "id" DESC LIMIT 12').fetchall()]
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "active": "admin",
            "field_registry": DROPDOWN_FIELD_REGISTRY,
            "selected_field": field_key,
            "options": options,
            "counts_by_field": counts_by_field,
            "db_info": get_db_status(),
            "all_backups": list_backups(),
            "recent_audit": recent_audit,
            "import_report": import_report,
            "restore_report": restore_report,
            "error_msg": error_msg,
            "restore_error_msg": restore_error_msg,
            "msg": msg,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, field_key: str = "wo_rig_name", msg: str = ""):
    return _render_admin_page(request, field_key=field_key, msg=msg)


@app.post("/admin/dropdown/add")
def admin_add_dropdown(field_key: str = Form(...), option_value: str = Form(...), sort_order: int = Form(100)):
    val = option_value.strip()
    flabel = DROPDOWN_FIELD_REGISTRY.get(field_key, field_key)
    if val:
        with db_session(write=True) as conn:
            conn.execute('INSERT OR IGNORE INTO "app_dropdown_options" ("field_key","field_label","option_value","sort_order") VALUES (?,?,?,?)', (field_key, flabel, val, sort_order))
            log_audit(conn, "ADD_DROPDOWN", "app_dropdown_options", f"{field_key}: {val}")
    return RedirectResponse(url=f"/admin?field_key={field_key}&msg=Added+option:+{val}", status_code=303)


@app.post("/admin/dropdown/delete/{opt_id}")
def admin_delete_dropdown(opt_id: int, field_key: str = Form("wo_rig_name")):
    with db_session(write=True) as conn:
        conn.execute('DELETE FROM "app_dropdown_options" WHERE "id"=?', (opt_id,))
        log_audit(conn, "DEL_DROPDOWN", "app_dropdown_options", f"id={opt_id}")
    return RedirectResponse(url=f"/admin?field_key={field_key}&msg=Option+removed", status_code=303)


@app.post("/admin/import-accdb", response_class=HTMLResponse)
async def admin_import_accdb(request: Request, accdb_file: UploadFile = File(...)):
    suffix = os.path.splitext(accdb_file.filename or "upload.accdb")[1] or ".accdb"
    fd, tmp_path = tempfile.mkstemp(prefix="sr_upload_", suffix=suffix)
    os.close(fd)
    import_report = None
    error_msg = ""
    try:
        content = await accdb_file.read()
        with open(tmp_path, "wb") as out:
            out.write(content)
        import_report = import_accdb_file(tmp_path, source_label=f"AdminUpload:{accdb_file.filename}")
    except Exception as e:
        error_msg = str(e)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    from app.database import normalize_all_dates
    with db_session(write=True) as conn:
        normalize_all_dates(conn)

    return _render_admin_page(
        request,
        import_report=import_report,
        error_msg=error_msg,
        msg=f"Imported {import_report['total_rows']} total rows from {accdb_file.filename}!" if import_report else "",
    )


@app.post("/admin/restore-backup", response_class=HTMLResponse)
def admin_restore_backup(request: Request, backup_filename: str = Form(...)):
    restore_report = None
    restore_error_msg = ""
    try:
        backup_path = resolve_server_backup_path(backup_filename)
        restore_report = restore_from_db_file(
            backup_path,
            source_label=get_current_username(),
            current_session_token=request.cookies.get(SESSION_COOKIE_NAME),
            display_name=os.path.basename(backup_path),
        )
    except Exception as e:
        restore_error_msg = str(e)

    return _render_admin_page(
        request,
        restore_report=restore_report,
        restore_error_msg=restore_error_msg,
        msg=(
            f"Database restored from server backup {restore_report['source_file']} "
            f"({restore_report['total_rows']} total rows)."
        )
        if restore_report
        else "",
    )


@app.post("/admin/restore-upload", response_class=HTMLResponse)
async def admin_restore_upload(request: Request, backup_file: UploadFile = File(...)):
    orig_name = backup_file.filename or "backup.db"
    suffix = os.path.splitext(orig_name)[1] or ".db"
    fd, tmp_path = tempfile.mkstemp(prefix="sr_restore_", suffix=suffix)
    os.close(fd)
    restore_report = None
    restore_error_msg = ""
    try:
        content = await backup_file.read()
        with open(tmp_path, "wb") as out:
            out.write(content)
        restore_report = restore_from_uploaded_backup(
            tmp_path,
            original_filename=orig_name,
            source_label=get_current_username(),
            current_session_token=request.cookies.get(SESSION_COOKIE_NAME),
        )
    except Exception as e:
        restore_error_msg = str(e)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return _render_admin_page(
        request,
        restore_report=restore_report,
        restore_error_msg=restore_error_msg,
        msg=(
            f"Reloaded {restore_report['total_rows']} total rows from {orig_name} "
            f"({restore_report['method']})."
        )
        if restore_report
        else "",
    )


# =============================================================================
# REPORTS & APIs
# =============================================================================
def _build_report_context(conn, report_type: str, well: str, run_date: str) -> dict:
    if report_type not in REPORT_DEFINITIONS:
        raise HTTPException(status_code=404, detail="Unknown report type")
    meta = REPORT_DEFINITIONS[report_type]
    params = [well]
    dclause = ' AND "WellRunDate" = ?' if run_date else ""
    if run_date:
        params.append(run_date)
    workover = conn.execute(f'SELECT * FROM "tblwoverdata" WHERE "WellName"=?{dclause} ORDER BY "WOverDate" DESC LIMIT 1', params).fetchone()
    pump = conn.execute(f'SELECT * FROM "tblpumpdata" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params).fetchone()
    tbg_master = conn.execute(f'SELECT * FROM "tblTBGdata" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params).fetchone()
    wear_rows = [dict(r) for r in conn.execute(f'SELECT * FROM "tblwearcommentsdatanew" WHERE "WellName"=?{dclause} ORDER BY CAST("intervalno" AS INTEGER), "id"', params).fetchall()]
    wear_meta = conn.execute(f'SELECT * FROM "tblwearcommentsdata2" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params).fetchone()
    summary = calculate_well_summary(conn, well, run_date or None)
    final_run = run_date or summary.get("run_date", "")
    cf_pages = REPORT_CUSTOM_PAGES.get(report_type, [])
    custom_fields = get_custom_values_map(conn, well, final_run, cf_pages) if (final_run and cf_pages) else []
    return {
        "report_type": report_type, "report_title": meta["title"], "report_category": meta["category"],
        "well_name": well, "run_date": run_date or summary.get("run_date", ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "workover": dict(workover) if workover else {},
        "pump": dict(pump) if pump else {},
        "tbg_master": dict(tbg_master) if tbg_master else {},
        "wear_rows": wear_rows,
        "wear_meta": dict(wear_meta) if wear_meta else {},
        "summary": summary,
        "custom_fields": custom_fields,
    }


@app.get("/reports", response_class=HTMLResponse)
def reports_hub(request: Request, well: str = "", run_date: str = ""):
    with db_session() as conn:
        wells = _get_all_wells(conn)
        selected_well = well.strip() or (wells[0] if wells else "")
        run_dates = _get_well_run_dates(conn, selected_well) if selected_well else []
    return templates.TemplateResponse("reports.html", {"request": request, "active": "reports", "wells": wells, "selected_well": selected_well, "run_dates": run_dates, "selected_date": run_date, "reports": REPORT_DEFINITIONS})


@app.get("/reports/view/{report_type}", response_class=HTMLResponse)
def report_view(request: Request, report_type: str, well: str = Query(...), run_date: str = ""):
    with db_session() as conn:
        ctx = _build_report_context(conn, report_type, well.strip(), run_date.strip())
    ctx["request"], ctx["active"] = request, "reports"
    return templates.TemplateResponse("report_view.html", ctx)


@app.get("/reports/excel/{report_type}")
def report_excel(report_type: str, well: str = Query(...), run_date: str = ""):
    with db_session() as conn:
        ctx = _build_report_context(conn, report_type, well.strip(), run_date.strip())
    xlsx_bytes = build_report_workbook(report_type, ctx)
    xlsx_bytes = append_custom_fields_to_xlsx(xlsx_bytes, ctx.get("custom_fields") or [])
    safe_well = well.replace("/", "_").replace(" ", "_")
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{report_type}_{safe_well}.xlsx"'},
    )


@app.get("/api/status")
def api_status():
    return JSONResponse(get_db_status())


@app.post("/api/backup")
def api_backup():
    return JSONResponse({"status": "ok", "backup_file": os.path.basename(create_backup())})


@app.get("/api/backup/excel")
def api_backup_excel(include_program: bool = False):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = export_db_to_excel_bytes(include_program=include_program)
    tag = "Full" if include_program else "Data"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="SR_Database_{tag}_{ts}.xlsx"'},
    )


@app.get("/api/backup/csv-zip")
def api_backup_csv_zip(include_program: bool = False):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = export_db_to_csv_zip_bytes(include_program=include_program)
    tag = "Full" if include_program else "Data"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="SR_Database_{tag}_CSV_{ts}.zip"'},
    )


@app.get("/api/export/new-pump-excel")
def api_export_new_pump_excel(well: str = ""):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    wn = well.strip()
    data = export_new_pump_table_excel_bytes(well=wn)
    suffix = f"_{wn.replace('/', '_').replace(' ', '_')}" if wn else "_All_Wells"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="New_DH_Pump_Data{suffix}_{ts}.xlsx"'},
    )


@app.get("/api/well-dates/{well_name:path}")
def api_well_dates(well_name: str):
    with db_session() as conn:
        d_set = []
        for sql in (
            'SELECT DISTINCT "WellRunDate" FROM "tblwelldata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
            'SELECT DISTINCT "WellRunDate" FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
            'SELECT DISTINCT "WellRunDate" FROM "tblTBGdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
            'SELECT DISTINCT "WellRunDate" FROM "tblpumpdata" WHERE "WellName"=? AND "WellRunDate" IS NOT NULL AND LENGTH(TRIM("WellRunDate")) > 0',
        ):
            for r in conn.execute(sql, (well_name,)).fetchall():
                if r[0] and r[0] not in d_set:
                    d_set.append(r[0])
        dates = sorted(d_set, key=lambda x: _parse_any_date(x) or datetime(1900, 1, 1), reverse=True)
    return JSONResponse({"well_name": well_name, "dates": dates})


@app.get("/api/workover-record")
def api_workover_record(well: str = Query(...), run_date: str = Query(...)):
    with db_session() as conn:
        row = conn.execute(
            'SELECT * FROM "tblwoverdata" WHERE "WellName"=? AND "WellRunDate"=? ORDER BY "_rowid_pk" DESC LIMIT 1',
            (well.strip(), run_date.strip()),
        ).fetchone()
    return JSONResponse({"found": bool(row), "record": dict(row) if row else {}})


@app.get("/api/offline-snapshot")
def api_offline_snapshot():
    """Lightweight JSON snapshot cached in IndexedDB for offline search & browsing."""
    with db_session() as conn:
        wells = [dict(r) for r in conn.execute('SELECT "id", "WellName", "notes" FROM "tblwells" ORDER BY "WellName"').fetchall()]
        runs = [dict(r) for r in conn.execute('SELECT "WellName", "WellRunDate" FROM "tblwelldata" ORDER BY "WellRunDate" DESC LIMIT 250').fetchall()]
        workovers = [
            dict(r)
            for r in conn.execute(
                'SELECT "WellName", "WellRunDate", "WOverDate", "WellLifeTime", "Wellfailurereason", '
                '"W/Oreason", "RIGNAMErods", "WellStatus" '
                'FROM "tblwoverdata" ORDER BY "WOverDate" DESC, "_rowid_pk" DESC LIMIT 250'
            ).fetchall()
        ]
        pumps = [
            dict(r)
            for r in conn.execute(
                'SELECT "WellName", "WellRunDate", "PumpSN", "Pumpdesignation", "Vendor", '
                '"PumpCOND", "StartDate", "FailureDate", "PumpLifetime" '
                'FROM "tblpumpdata" ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC LIMIT 250'
            ).fetchall()
        ]
    return JSONResponse(
        {
            "ok": True,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "wells": wells,
            "runs": runs,
            "workovers": workovers,
            "pumps": pumps,
        }
    )


@app.get("/sw.js")
def pwa_service_worker():
    sw_path = get_resource_path(os.path.join("app", "static", "sw.js"))
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
def pwa_manifest():
    manifest_path = get_resource_path(os.path.join("app", "static", "manifest.json"))
    return FileResponse(manifest_path, media_type="application/manifest+json")


@app.get("/favicon.ico")
def pwa_favicon():
    ico_path = get_resource_path(os.path.join("app", "static", "icon.ico"))
    return FileResponse(ico_path, media_type="image/x-icon")
