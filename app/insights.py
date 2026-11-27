"""Enhancements #2 and #3.

#2  Well Failure, Wear Profile, Pump & Rod String History -> GET /api/well-history (+ static/well_history.js)
#3  Dashboard "Finished Reports" view                     -> get_dashboard_runs()  (used by "/" in main.py)
Admin settings for both                                   -> /admin/insights
"""
import json
import os
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import db_session, get_resource_path, log_audit, _to_iso_date_str
from app.calculations import calculate_well_summary

router = APIRouter()
templates = Jinja2Templates(directory=get_resource_path(os.path.join("app", "templates")))

# --------------------------------------------------------------------------- registries
# (key, label, source, column)  source: "wo" = tblwoverdata, "pump" = tblpumpdata
FAILURE_COLUMNS = [
    ("life", "Lifetime (d)", "wo", "WellLifeTime"),
    ("wo_reason", "W/O Reason", "wo", "W/Oreason"),
    ("failure", "Well-Failure Reason", "wo", "Wellfailurereason"),
    ("pump_fail", "Pump Failure", "wo", "pumpfailure type"),
    ("tbg_fail", "TBG Failure", "wo", "tbgfailuretype"),
    ("tbg_no", "Defected TBG #", "wo", "defectedtbgno"),
    ("tbg_depth", "TBG Fail Depth", "wo", "tbgfailuredepth"),
    ("rod_fail", "Rod Failure", "wo", "rodfailuretype"),
    ("parted_no", "Parted Rod #", "wo", "Partedrodnofromsurface"),
    ("parted_size", "Parted Size", "wo", "Partedrodsize"),
    ("parted_grade", "Parted Grade", "wo", "Partedrodgrade"),
    ("parted_depth", "Part Depth", "wo", "Partdepth"),
    ("scale", "Scale", "wo", "Scalepresence"),
    ("wear_sum", "Wear Summary", "wo", "Wearcomments"),
    ("well_status", "Well Status", "wo", "WellStatus"),
    ("spm", "SPM", "wo", "Wellspeed"),
    ("sl", "SL (in)", "wo", "SL"),
    ("wc", "W/C (%)", "wo", "W/C"),
    ("h2s", "H2S (PPM)", "wo", "H2S(PPM)"),
    ("pump_sn", "Pump SN", "pump", "PumpSN"),
    ("pump_design", "Pump Design", "pump", "Pumpdesignation"),
    ("pump_insp", "Pump Insp. Cond.", "pump", "pumpinspectioncond"),
    ("pump_life", "Pump Lifetime (d)", "pump", "PumpLifetime"),
]
DEFAULT_FAILURE_KEYS = ["life", "wo_reason", "failure", "pump_fail", "tbg_fail", "rod_fail", "parted_no", "parted_depth"]

# (key, label, short chip)
STEPS = [
    ("workover", "Workover & Pulled Data", "W/O"),
    ("wear", "Pulled Rod Wear", "Wear"),
    ("tbg", "New Tubing", "TBG"),
    ("pump", "New Downhole Pump", "Pump"),
    ("rods", "New Rod String", "Rods"),
    ("teardown", "Pump Tear Down", "TD"),
]
WEAR_COMPONENTS = ["Coupling", "Shoulder", "Neck", "Body", "Guides", "Wearrate"]
_SEV_RANK = {"": 0, "-": 0, "none": 0, "n/a": 0, "light": 1, "slight": 2, "moderate": 3, "severe": 4}


# --------------------------------------------------------------------------- settings
def get_setting(conn, key: str, default=None):
    try:
        r = conn.execute('SELECT "value" FROM "app_settings" WHERE "key"=?', (key,)).fetchone()
    except Exception:
        return default
    return r["value"] if r and r["value"] is not None else default


def get_setting_json(conn, key: str, default):
    raw = get_setting(conn, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        'INSERT INTO "app_settings" ("key","value") VALUES (?,?) '
        'ON CONFLICT("key") DO UPDATE SET "value"=excluded."value","updated_at"=CURRENT_TIMESTAMP',
        (key, value if isinstance(value, str) else json.dumps(value)))


def _txt(v) -> str:
    return "" if v is None else str(v).strip()


def _num_txt(v) -> str:
    s = _txt(v)
    try:
        f = float(s)
        return str(int(f)) if f == int(f) else str(round(f, 2))
    except ValueError:
        return s


def _to_float(v):
    s = _txt(v).replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _sev(v) -> int:
    s = _txt(v).lower()
    return _SEV_RANK.get(s, 1)  # unknown (admin-added) non-dash value counts as at least "light"


def _iso(v) -> str:
    return _to_iso_date_str(v) or ""


# --------------------------------------------------------------------------- #2 history
def build_well_history(conn, well: str) -> dict:
    well = well.strip()
    keys = get_setting_json(conn, "history_failure_columns", DEFAULT_FAILURE_KEYS)
    try:
        max_runs = max(1, min(50, int(get_setting(conn, "history_max_runs", "10"))))
    except ValueError:
        max_runs = 10
    cols = [c for c in FAILURE_COLUMNS if c[0] in keys]

    # ---- failures: one row per run, newest first
    wo_by_run = {}
    for r in conn.execute('SELECT * FROM "tblwoverdata" WHERE "WellName"=? ORDER BY "_rowid_pk" DESC', (well,)).fetchall():
        d = _iso(r["WellRunDate"])
        if d and d not in wo_by_run:
            wo_by_run[d] = dict(r)
    pump_by_run = {}
    for r in conn.execute('SELECT * FROM "tblpumpdata" WHERE "WellName"=? ORDER BY "_rowid_pk" DESC', (well,)).fetchall():
        d = _iso(r["WellRunDate"])
        if d and d not in pump_by_run:
            pump_by_run[d] = dict(r)
    run_keys = sorted(set(wo_by_run) | (set(pump_by_run) if any(c[2] == "pump" for c in cols) else set()), reverse=True)
    failures = []
    for d in run_keys[:max_runs]:
        wo, pm = wo_by_run.get(d, {}), pump_by_run.get(d, {})
        cells = []
        for _k, _l, src, col in cols:
            v = (wo if src == "wo" else pm).get(col)
            cells.append(_num_txt(v) if col in ("WellLifeTime", "PumpLifetime") else _txt(v))
        failures.append({"run_date": d, "wo_date": _iso(wo.get("WOverDate")), "cells": cells})

    # ---- wear: one entry per run + numeric depth profile for side-view erosion plot
    wear_runs = {_iso(r[0]) for r in conn.execute(
        'SELECT DISTINCT "WellRunDate" FROM "tblwearcommentsdatanew" WHERE "WellName"=?', (well,)).fetchall() if r[0]}
    wear_runs |= {_iso(r[0]) for r in conn.execute(
        'SELECT DISTINCT "WellRunDate" FROM "tblwearcommentsdata2" WHERE "WellName"=?', (well,)).fetchall() if r[0]}
    wear_runs.discard("")
    wear = []
    rows_by_run, meta_by_run = {}, {}
    for r in conn.execute('SELECT * FROM "tblwearcommentsdatanew" WHERE "WellName"=? ORDER BY CAST("intervalno" AS INTEGER),"id"', (well,)).fetchall():
        rows_by_run.setdefault(_iso(r["WellRunDate"]), []).append(dict(r))
    for r in conn.execute('SELECT "Pitting","General Comments","WellRunDate" FROM "tblwearcommentsdata2" WHERE "WellName"=? ORDER BY "_rowid_pk" DESC', (well,)).fetchall():
        meta_by_run.setdefault(_iso(r["WellRunDate"]), dict(r))

    for d in sorted(wear_runs, reverse=True)[:max_runs]:
        rows, meta = rows_by_run.get(d, []), meta_by_run.get(d, {})
        worn, intervals, worst = [], [], 0
        for s in rows:
            ranks = {c: _sev(s.get(c)) for c in WEAR_COMPONENTS}
            top = max(ranks.values()) if ranks else 0
            rod_lbl = " ".join(x for x in (_txt(s.get("Rodtype")), _txt(s.get("Rodgrade"))) if x)
            fd, td = _to_float(s.get("FDepth")), _to_float(s.get("TDepth"))
            if fd is not None and td is not None and td != fd:
                if fd > td:
                    fd, td = td, fd
                intervals.append({
                    "interval": _txt(s.get("intervalno")),
                    "rod": rod_lbl,
                    "f_depth": round(fd, 1),
                    "t_depth": round(td, 1),
                    "max_rank": top,
                    "ranks": ranks,
                    "raw": {c: _txt(s.get(c)) or "-" for c in WEAR_COMPONENTS},
                })
            if top > 0:
                worst = max(worst, top)
                worn.append({
                    "interval": _txt(s.get("intervalno")),
                    "rod": rod_lbl,
                    "depth": "–".join(x for x in (_num_txt(s.get("FDepth")), _num_txt(s.get("TDepth"))) if x),
                    "detail": ", ".join(f"{c if c != 'Wearrate' else 'Rate'}: {_txt(s.get(c))}" for c in WEAR_COMPONENTS if ranks[c] > 0),
                    "rank": top,
                })
        wear.append({
            "run_date": d, "sections": len(rows), "worn_count": len(worn), "worst": worst,
            "pitting": _txt(meta.get("Pitting")), "comments": _txt(meta.get("General Comments")),
            "worn": worn, "intervals": intervals,
        })

    # ---- DH Pump History across previous runs
    pumps = []
    for d in sorted(pump_by_run, reverse=True)[:max_runs]:
        pm = pump_by_run[d]
        pumps.append({
            "run_date": d,
            "pump_sn": _txt(pm.get("PumpSN")),
            "designation": _txt(pm.get("Pumpdesignation")),
            "vendor": _txt(pm.get("Vendor")),
            "clearance": _txt(pm.get("PlungerClearance")),
            "cond": _txt(pm.get("PumpCOND")),
            "start_date": _iso(pm.get("StartDate")),
            "failure_date": _iso(pm.get("FailureDate")),
            "lifetime": _num_txt(pm.get("PumpLifetime")),
            "insp_cond": _txt(pm.get("pumpinspectioncond")),
            "has_teardown": bool(_txt(pm.get("pumpreportdate")) or _txt(pm.get("inspectby"))),
        })

    # ---- Rod String History across previous runs (supports both new & legacy Access runs)
    rod_run_dates = set()
    for tbl in ("tblrodsdatanew", "tblrodstringdata1", "tblrodstringdata2"):
        for r in conn.execute(f'SELECT DISTINCT "WellRunDate" FROM "{tbl}" WHERE "WellName"=?', (well,)).fetchall():
            d = _iso(r[0])
            if d:
                rod_run_dates.add(d)
    rod_strings = []
    for d in sorted(rod_run_dates, reverse=True)[:max_runs]:
        try:
            s = calculate_well_summary(conn, well, d)
        except Exception:
            continue
        rod_rows = s.get("rod_rows") or []
        intake_rows = s.get("intake_rows") or []
        if not rod_rows and not intake_rows:
            continue
        taper_parts = []
        for r in rod_rows:
            itype = _txt(r.get("itemtype"))
            if not itype or any(skip in itype.lower() for skip in ("pony", "polished", "stab")):
                continue
            no_v = _num_txt(r.get("itemno"))
            gr = _txt(r.get("itemsize"))
            taper_parts.append(f"{no_v}× {itype}" + (f" ({gr})" if gr else ""))
        intake_parts = []
        for it in intake_rows:
            itype = _txt(it.get("itemtype"))
            ln = _num_txt(it.get("itemlength"))
            sz = _txt(it.get("itemsize"))
            intake_parts.append(f"{itype}" + (f" {ln} ft" if ln else "") + (f" ({sz})" if sz else ""))
        rm = s.get("rod_meta") or {}
        rod_strings.append({
            "run_date": d,
            "source": s.get("rod_source", "new"),
            "rig": _txt(rm.get("RigName")),
            "rotator": _txt(rm.get("RodRotator")),
            "total_count": s.get("rod_total_count", 0),
            "total_length": s.get("rod_total_length", 0),
            "weight_air": s.get("rod_weight_air_lbs", 0),
            "tapers": " · ".join(taper_parts) if taper_parts else f"{len(rod_rows)} items",
            "intake": ", ".join(intake_parts),
        })

    return {
        "ok": True, "well": well, "max_runs": max_runs,
        "columns": [{"key": c[0], "label": c[1]} for c in cols],
        "failures": failures, "wear": wear,
        "pumps": pumps, "rod_strings": rod_strings,
    }


@router.get("/api/well-history")
def api_well_history(well: str = Query("")):
    if not well.strip():
        return JSONResponse({"ok": False, "error": "No well"}, status_code=400)
    with db_session() as conn:
        return build_well_history(conn, well)


# --------------------------------------------------------------------------- #3 dashboard
_RUN_STATUS_SQL = """
SELECT w."_rowid_pk" AS pk, w."WellName" AS WellName, w."WellRunDate" AS WellRunDate, w."WOverDate" AS WOverDate,
       w."WellLifeTime" AS WellLifeTime, w."W/Oreason" AS WOreason, w."Wellfailurereason" AS Failure,
  EXISTS(SELECT 1 FROM "tblwearcommentsdatanew" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate") AS s_wear,
  (EXISTS(SELECT 1 FROM "tblTBGdata" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate")
   OR EXISTS(SELECT 1 FROM "tblTBGdatanew" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate")) AS s_tbg,
  (EXISTS(SELECT 1 FROM "tblrodstringdata2" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate")
   OR EXISTS(SELECT 1 FROM "tblrodsdatanew" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate")
   OR EXISTS(SELECT 1 FROM "tblrodstringdata1" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate")) AS s_rods,
  EXISTS(SELECT 1 FROM "tblpumpdata" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate") AS s_pump,
  EXISTS(SELECT 1 FROM "tblpumpdata" x WHERE x."WellName"=w."WellName" AND x."WellRunDate"=w."WellRunDate"
         AND (TRIM(COALESCE(x."pumpreportdate",''))<>'' OR TRIM(COALESCE(x."inspectby",''))<>'')) AS s_teardown
FROM "tblwoverdata" w
WHERE w."WellName" IS NOT NULL AND TRIM(w."WellName")<>''
ORDER BY (w."WOverDate" IS NULL OR w."WOverDate"=''), w."WOverDate" DESC, w."_rowid_pk" DESC
"""


def get_required_steps(conn) -> List[str]:
    valid = [s[0] for s in STEPS]
    req = get_setting_json(conn, "finished_required_steps", valid)
    req = [k for k in req if k in valid]
    return req or valid


def get_dashboard_runs(conn, status: str = "all", limit: int = 15) -> dict:
    required = set(get_required_steps(conn))
    seen, runs = set(), []
    for r in conn.execute(_RUN_STATUS_SQL).fetchall():
        ident = (r["WellName"], r["WellRunDate"] or f"pk{r['pk']}")
        if ident in seen:
            continue
        seen.add(ident)
        steps = {
            "workover": True, "wear": bool(r["s_wear"]), "tbg": bool(r["s_tbg"]),
            "pump": bool(r["s_pump"]), "rods": bool(r["s_rods"]), "teardown": bool(r["s_teardown"]),
        }
        done = sum(1 for k in required if steps[k])
        finished = done == len(required)
        has_run = bool(r["WellRunDate"])
        runs.append({
            "well": r["WellName"], "run_date": r["WellRunDate"] or "", "wo_date": r["WOverDate"] or "",
            "lifetime": r["WellLifeTime"], "reason": r["Failure"] or r["WOreason"] or "",
            "steps": steps, "done": done, "total": len(required), "finished": finished,
            "reports": [
                {"label": "Wellbore Sketch", "type": "rptwellboresketch", "enabled": has_run and steps["tbg"] and steps["rods"],
                 "need": "needs Tubing + Rod String"},
                {"label": "Work Over Report", "type": "rptwoverwwear" if steps["wear"] else "rptwoverwowear", "enabled": has_run,
                 "need": ""},
                {"label": "Pump Tear Down", "type": "rptpumpreport", "enabled": has_run and steps["teardown"],
                 "need": "needs Tear Down inspection"},
            ],
        })
    counts = {"total": len(runs), "finished": sum(1 for x in runs if x["finished"])}
    counts["progress"] = counts["total"] - counts["finished"]
    shown = [x for x in runs if status == "all" or (status == "finished") == x["finished"]]
    labels = ", ".join(lbl for key, lbl, _c in STEPS if key in required)
    return {"runs": shown[:limit], "counts": counts, "required": sorted(required), "status": status,
            "total_required_labels": labels}


# --------------------------------------------------------------------------- admin settings page
@router.get("/admin/insights", response_class=HTMLResponse)
def insights_admin(request: Request, msg: str = ""):
    with db_session() as conn:
        selected = get_setting_json(conn, "history_failure_columns", DEFAULT_FAILURE_KEYS)
        max_runs = get_setting(conn, "history_max_runs", "10")
        required = get_required_steps(conn)
    return templates.TemplateResponse(request=request, name="admin_insights.html", context={
        "request": request, "active": "admin", "msg": msg,
        "columns": FAILURE_COLUMNS, "selected": selected, "max_runs": max_runs,
        "steps": STEPS, "required": required,
    })


@router.post("/admin/insights/save")
def insights_save(fail_cols: List[str] = Form([]), max_runs: int = Form(10), req_steps: List[str] = Form([])):
    valid_cols = {c[0] for c in FAILURE_COLUMNS}
    valid_steps = {s[0] for s in STEPS}
    cols = [c for c in fail_cols if c in valid_cols] or DEFAULT_FAILURE_KEYS
    steps = [s for s in req_steps if s in valid_steps] or [s[0] for s in STEPS]
    with db_session(write=True) as conn:
        set_setting(conn, "history_failure_columns", cols)
        set_setting(conn, "history_max_runs", str(max(1, min(50, max_runs))))
        set_setting(conn, "finished_required_steps", steps)
        log_audit(conn, "SAVE_SETTINGS", "app_settings", f"history cols={len(cols)} runs={max_runs} required={len(steps)}")
    return RedirectResponse(url=f"/admin/insights?msg={quote('Settings saved')}", status_code=303)
