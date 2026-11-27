"""Dynamic Page Layout & Custom Fields (Option A — External JSON + Unified Metadata Registry).

- Loads built-in sections and fields once from external data/builtin_layout.json (cached in memory).
- Auto-copies bundled app/builtin_layout.json to data/builtin_layout.json on first run.
- Seeds them into app_builtin_fields so Admin can reorder, rename, or hide/show built-in fields.
- Lets Admin add custom fields (text/number/date/dropdown) into ANY section at ANY sort_order.
- Input pages load /api/page-layout via static/custom_fields.js to arrange sections & inject fields.
"""
import json
import os
import re
import shutil
from typing import Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import app.database as _db
from app.database import db_session, get_resource_path, log_audit, _to_iso_date_str, get_current_username

router = APIRouter()
templates = Jinja2Templates(directory=get_resource_path(os.path.join("app", "templates")))

PAGES = {
    "workover": "1. Workover & Pulled Data",
    "wear": "2. Pulled Rod Wear",
    "tbg": "3. New Tubing (TBG)",
    "pumps": "4. New Downhole Pump",
    "rods": "5. New Rod String",
    "teardown": "6. Pump Tear Down",
}
INPUT_TYPES = {"text": "Text", "number": "Number", "date": "Date", "dropdown": "Dropdown"}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BUILTIN_LAYOUT_CACHE = None
_SCHEMA_READY = False


def get_builtin_layout(reload: bool = False) -> dict:
    """Load external data/builtin_layout.json (auto-copied from bundled default on first run) and cache in memory."""
    global _BUILTIN_LAYOUT_CACHE
    if reload:
        _BUILTIN_LAYOUT_CACHE = None
    if _BUILTIN_LAYOUT_CACHE is None:
        os.makedirs(_db.DATA_DIR, exist_ok=True)
        external_path = os.path.join(_db.DATA_DIR, "builtin_layout.json")
        bundled_path = get_resource_path(os.path.join("app", "builtin_layout.json"))
        if not os.path.exists(external_path) and os.path.exists(bundled_path):
            shutil.copy2(bundled_path, external_path)
        target_path = external_path if os.path.exists(external_path) else bundled_path
        with open(target_path, "r", encoding="utf-8") as f:
            _BUILTIN_LAYOUT_CACHE = json.load(f)
    return _BUILTIN_LAYOUT_CACHE


def _ensure_layout_schema(conn):
    """Idempotently add section_key/placeholder/default_value and seed app_builtin_fields from JSON."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cols = {r[1] for r in conn.execute('PRAGMA table_info("app_custom_fields")').fetchall()}
    if "section_key" not in cols:
        conn.execute('ALTER TABLE "app_custom_fields" ADD COLUMN "section_key" TEXT NOT NULL DEFAULT \'\'')
    if "placeholder" not in cols:
        conn.execute('ALTER TABLE "app_custom_fields" ADD COLUMN "placeholder" TEXT NOT NULL DEFAULT \'\'')
    if "default_value" not in cols:
        conn.execute('ALTER TABLE "app_custom_fields" ADD COLUMN "default_value" TEXT NOT NULL DEFAULT \'\'')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS "app_builtin_fields" (
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
        )'''
    )
    bcols = {r[1] for r in conn.execute('PRAGMA table_info("app_builtin_fields")').fetchall()}
    added_placeholder = "placeholder" not in bcols
    if added_placeholder:
        conn.execute('ALTER TABLE "app_builtin_fields" ADD COLUMN "placeholder" TEXT NOT NULL DEFAULT \'\'')
    if "default_value" not in bcols:
        conn.execute('ALTER TABLE "app_builtin_fields" ADD COLUMN "default_value" TEXT NOT NULL DEFAULT \'\'')

    layout = get_builtin_layout()
    rows = []
    backfill = []
    for page_key, sec_list in layout.items():
        for sec in sec_list:
            sec_key = sec["key"]
            for idx, fld in enumerate(sec.get("fields", []), 1):
                ph = str(fld.get("placeholder") or "")
                dv = str(fld.get("default_value") or "")
                ord_v = int(fld.get("order") or (idx * 10))
                act_v = 1 if fld.get("active", True) else 0
                rows.append((page_key, sec_key, fld["name"], fld["label"], fld.get("type", "text"), ord_v, act_v, ph, dv))
                if added_placeholder and (ph or dv):
                    backfill.append((ph, dv, page_key, fld["name"]))
    conn.executemany(
        'INSERT OR IGNORE INTO "app_builtin_fields" '
        '("target_page","section_key","input_name","label","input_type","sort_order","active","placeholder","default_value") '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        rows,
    )
    if backfill:
        conn.executemany(
            'UPDATE "app_builtin_fields" SET "placeholder"=?, "default_value"=? WHERE "target_page"=? AND "input_name"=?',
            backfill,
        )

    # Migrate legacy single 'tbg_header' section to grouped Step 3 cards if present
    legacy_tbg = conn.execute(
        'SELECT COUNT(*) FROM "app_builtin_fields" WHERE "target_page"=\'tbg\' AND "section_key"=\'tbg_header\''
    ).fetchone()[0]
    if legacy_tbg:
        tbg_sec_map = []
        for sec in layout.get("tbg", []):
            sk = sec["key"]
            for idx, fld in enumerate(sec.get("fields", []), 1):
                tbg_sec_map.append((sk, int(fld.get("order") or (idx * 10)), "tbg", fld["name"]))
        if tbg_sec_map:
            conn.executemany(
                'UPDATE "app_builtin_fields" SET "section_key"=?, "sort_order"=? WHERE "target_page"=? AND "input_name"=?',
                tbg_sec_map,
            )
            conn.execute(
                'UPDATE "app_custom_fields" SET "section_key"=\'tbg_comp_depths\' WHERE "target_page"=\'tbg\' AND "section_key"=\'tbg_header\''
            )
    # Keep PlugSeat inside Valves & Cages (pump_valves) for existing DBs
    conn.execute(
        'UPDATE "app_builtin_fields" SET "section_key"=\'pump_valves\' WHERE "target_page"=\'pumps\' AND "input_name"=\'PlugSeat\' AND "section_key"=\'pump_specs\''
    )
    conn.commit()
    _SCHEMA_READY = True


def save_builtin_layout_to_json(conn) -> str:
    """Persist current app_builtin_fields state to external data/builtin_layout.json."""
    global _BUILTIN_LAYOUT_CACHE
    base_layout = get_builtin_layout()
    rows = [
        dict(r)
        for r in conn.execute(
            'SELECT * FROM "app_builtin_fields" ORDER BY "target_page", "section_key", "sort_order", "id"'
        ).fetchall()
    ]
    by_sec = {}
    for r in rows:
        by_sec.setdefault((r["target_page"], r["section_key"]), []).append(r)

    new_layout = {}
    for page_key, sec_list in base_layout.items():
        new_secs = []
        for sec in sec_list:
            sec_key = sec["key"]
            sec_rows = by_sec.get((page_key, sec_key), [])
            fields_out = []
            for r in sec_rows:
                item = {
                    "name": r["input_name"],
                    "label": r["label"],
                    "type": r["input_type"],
                    "order": int(r["sort_order"] or 10),
                    "active": bool(r["active"]),
                }
                if r.get("placeholder"):
                    item["placeholder"] = r["placeholder"]
                if r.get("default_value"):
                    item["default_value"] = r["default_value"]
                fields_out.append(item)
            new_secs.append({
                "key": sec_key,
                "title": sec["title"],
                "fields": fields_out,
            })
        new_layout[page_key] = new_secs

    os.makedirs(_db.DATA_DIR, exist_ok=True)
    external_path = os.path.join(_db.DATA_DIR, "builtin_layout.json")
    tmp_path = external_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(new_layout, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, external_path)
    _BUILTIN_LAYOUT_CACHE = new_layout
    return external_path


def sync_builtin_layout_from_json(conn) -> int:
    """Reload data/builtin_layout.json and sync labels, order, active state, placeholders, and defaults into app_builtin_fields."""
    _ensure_layout_schema(conn)
    layout = get_builtin_layout(reload=True)
    updated = 0
    for page_key, sec_list in layout.items():
        for sec in sec_list:
            sec_key = sec["key"]
            for idx, fld in enumerate(sec.get("fields", []), 1):
                ph = str(fld.get("placeholder") or "")
                dv = str(fld.get("default_value") or "")
                ord_v = int(fld.get("order") or (idx * 10))
                act_v = 1 if fld.get("active", True) else 0
                cur = conn.execute(
                    'UPDATE "app_builtin_fields" SET "section_key"=?, "label"=?, "input_type"=?, '
                    '"sort_order"=?, "active"=?, "placeholder"=?, "default_value"=? '
                    'WHERE "target_page"=? AND "input_name"=?',
                    (sec_key, fld["label"], fld.get("type", "text"), ord_v, act_v, ph, dv, page_key, fld["name"]),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        'INSERT OR IGNORE INTO "app_builtin_fields" '
                        '("target_page","section_key","input_name","label","input_type","sort_order","active","placeholder","default_value") '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (page_key, sec_key, fld["name"], fld["label"], fld.get("type", "text"), ord_v, act_v, ph, dv),
                    )
                updated += 1
    return updated


def _sections_for_page(page: str) -> list:
    secs = [{"key": sec["key"], "title": sec["title"]} for sec in get_builtin_layout().get(page, [])]
    secs.append({"key": "__bottom__", "title": "Bottom Custom Fields Card (Separate)"})
    return secs


def _valid_section_keys(page: str) -> set:
    return {s["key"] for s in _sections_for_page(page)}


# ---------------------------------------------------------------- helpers
def _slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return "cf_" + (s or "field")


def _unique_key(conn, label: str) -> str:
    base = _slugify(label)[:48]
    key, n = base, 2
    while conn.execute('SELECT 1 FROM "app_custom_fields" WHERE "field_key"=?', (key,)).fetchone():
        key = f"{base}_{n}"
        n += 1
    return key


def _parse_options(raw: str) -> list:
    seen, out = set(), []
    for line in (raw or "").replace(",", "\n").splitlines():
        v = line.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def _row_to_field(r) -> dict:
    d = dict(r)
    try:
        d["options"] = json.loads(d.get("options_json") or "[]")
    except ValueError:
        d["options"] = []
    if not d.get("section_key"):
        d["section_key"] = "__bottom__"
    return d


def get_fields(conn, page: Optional[str] = None, include_inactive: bool = False) -> list:
    _ensure_layout_schema(conn)
    sql, params = 'SELECT * FROM "app_custom_fields" WHERE 1=1', []
    if page:
        sql += ' AND "target_page"=?'
        params.append(page)
    if not include_inactive:
        sql += ' AND "active"=1'
    sql += ' ORDER BY "target_page","sort_order","id"'
    return [_row_to_field(r) for r in conn.execute(sql, params).fetchall()]


def get_custom_values_map(conn, well: str, run_date: str, page: Optional[Union[str, list]] = None) -> list:
    """Saved custom values for one run (used by the PDF/Excel reports)."""
    _ensure_layout_schema(conn)
    rd = _to_iso_date_str(run_date) or ""
    sql = ('SELECT f.label, f.input_type, f.target_page, f.field_key, v.value FROM "app_custom_values" v '
           'JOIN "app_custom_fields" f ON f.id=v.field_id '
           'WHERE v.WellName=? AND v.WellRunDate=? AND f.active=1 AND v.value IS NOT NULL AND v.value<>""')
    params = [well.strip(), rd]
    pages = [page] if isinstance(page, str) else list(page or [])
    if pages:
        sql += ' AND f.target_page IN (%s)' % ",".join("?" * len(pages))
        params += pages
    sql += ' ORDER BY f.target_page, f.sort_order, f.id'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def append_custom_fields_to_xlsx(xlsx_bytes: bytes, custom_fields: list) -> bytes:
    """Add an 'Additional Custom Fields' block under the first sheet of a generated report workbook."""
    if not custom_fields:
        return xlsx_bytes
    import io
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb.worksheets[0]
    r = ws.max_row + 2
    ws.cell(row=r, column=1, value="Additional Custom Fields").font = Font(bold=True, color="FFFFFF")
    for col in (1, 2):
        ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor="0F2544")
    for i, c in enumerate(custom_fields, 1):
        ws.cell(row=r + i, column=1, value=c["label"]).font = Font(bold=True)
        v = c["value"]
        if c.get("input_type") == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                pass
        ws.cell(row=r + i, column=2, value=v)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _validate(field: dict, raw) -> tuple:
    v = "" if raw is None else str(raw).strip()
    if not v:
        return ("", "Required" if field["required"] else None)
    t = field["input_type"]
    if t == "number":
        try:
            float(v.replace(",", ""))
        except ValueError:
            return (v, "Must be a number")
        return (v.replace(",", ""), None)
    if t == "date":
        iso = _to_iso_date_str(v)
        return (iso, None) if iso and _ISO.match(iso) else (v, "Invalid date")
    if t == "dropdown" and v not in field["options"]:
        return (v, "Not in list")
    return (v, None)


def _msg(url: str, msg: str, page: str = "") -> RedirectResponse:
    extra = f"&page={quote(page)}" if page else ""
    return RedirectResponse(url=f"{url}?msg={quote(msg)}{extra}", status_code=303)


# ---------------------------------------------------------------- admin UI
@router.get("/admin/custom-fields", response_class=HTMLResponse)
def custom_fields_admin(request: Request, page: str = "workover", msg: str = ""):
    selected_page = page if page in PAGES else "workover"
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        all_custom = get_fields(conn, include_inactive=True)
        val_counts = {r["field_id"]: r["c"] for r in conn.execute(
            'SELECT "field_id", COUNT(*) AS c FROM "app_custom_values" GROUP BY "field_id"').fetchall()}
        builtins = [dict(r) for r in conn.execute(
            'SELECT * FROM "app_builtin_fields" WHERE "target_page"=? ORDER BY "section_key","sort_order","id"',
            (selected_page,)).fetchall()]
        builtin_counts = {r["target_page"]: r["c"] for r in conn.execute(
            'SELECT "target_page", COUNT(*) AS c FROM "app_builtin_fields" GROUP BY "target_page"').fetchall()}

    custom_counts = {k: 0 for k in PAGES}
    page_custom = []
    for f in all_custom:
        f["value_count"] = val_counts.get(f["id"], 0)
        f["options_text"] = "\n".join(f["options"])
        custom_counts[f["target_page"]] = custom_counts.get(f["target_page"], 0) + 1
        if f["target_page"] == selected_page:
            page_custom.append(f)

    sections = []
    valid_keys = _valid_section_keys(selected_page)
    for sec in _sections_for_page(selected_page):
        sk = sec["key"]
        items = []
        for b in builtins:
            if b["section_key"] == sk:
                items.append({
                    "kind": "builtin", "id": b["id"], "key": b["input_name"],
                    "label": b["label"], "input_type": b["input_type"],
                    "sort_order": b["sort_order"], "active": bool(b["active"]),
                    "required": False, "options": [], "options_text": "",
                    "value_count": None, "section_key": sk,
                    "placeholder": b.get("placeholder") or "",
                    "default_value": b.get("default_value") or "",
                })
        for c in page_custom:
            c_sec = c["section_key"] if c["section_key"] in valid_keys else "__bottom__"
            if c_sec == sk:
                items.append({
                    "kind": "custom", "id": c["id"], "key": c["field_key"],
                    "label": c["label"], "input_type": c["input_type"],
                    "sort_order": c["sort_order"], "active": bool(c["active"]),
                    "required": bool(c["required"]), "options": c["options"],
                    "options_text": c["options_text"], "value_count": c["value_count"],
                    "section_key": c_sec,
                    "placeholder": c.get("placeholder") or "",
                    "default_value": c.get("default_value") or "",
                })
        items.sort(key=lambda x: (int(x["sort_order"] or 0), 0 if x["kind"] == "builtin" else 1, x["id"]))
        max_ord = max((int(x["sort_order"] or 0) for x in items), default=0)
        sections.append({
            "key": sk, "title": sec["title"], "items": items,
            "next_order": ((max_ord // 10) + 1) * 10,
        })

    return templates.TemplateResponse(request=request, name="admin_custom_fields.html", context={
        "request": request, "active": "admin", "msg": msg,
        "pages": PAGES, "input_types": INPUT_TYPES,
        "selected_page": selected_page,
        "sections": sections,
        "builtin_counts": builtin_counts,
        "custom_counts": custom_counts,
    })


@router.post("/admin/custom-fields/add")
def custom_fields_add(label: str = Form(...), input_type: str = Form(...), target_page: str = Form(...),
                      section_key: str = Form("__bottom__"), options: str = Form(""),
                      required: str = Form(""), sort_order: int = Form(10),
                      placeholder: str = Form(""), default_value: str = Form("")):
    label = label.strip()
    if not label or input_type not in INPUT_TYPES or target_page not in PAGES:
        return _msg("/admin/custom-fields", "Invalid field definition", target_page)
    if section_key not in _valid_section_keys(target_page):
        section_key = "__bottom__"
    opts = _parse_options(options) if input_type == "dropdown" else []
    if input_type == "dropdown" and not opts:
        return _msg("/admin/custom-fields", "Dropdown needs at least one option", target_page)
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        key = _unique_key(conn, label)
        conn.execute(
            'INSERT INTO "app_custom_fields" ("field_key","label","input_type","target_page","section_key","options_json","required","sort_order","placeholder","default_value") '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (key, label, input_type, target_page, section_key, json.dumps(opts), 1 if required else 0, sort_order, placeholder.strip(), default_value.strip()))
        log_audit(conn, "ADD_CUSTOM_FIELD", "app_custom_fields", f"{target_page}/{section_key}: {label} (order={sort_order})")
    return _msg("/admin/custom-fields", f"Added '{label}' at order {sort_order}", target_page)


@router.post("/admin/custom-fields/update/{field_id}")
def custom_fields_update(field_id: int, label: str = Form(...), section_key: str = Form("__bottom__"),
                         options: str = Form(""), required: str = Form(""), sort_order: int = Form(10),
                         placeholder: str = Form(""), default_value: str = Form("")):
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        row = conn.execute('SELECT * FROM "app_custom_fields" WHERE "id"=?', (field_id,)).fetchone()
        if not row:
            return _msg("/admin/custom-fields", "Field not found")
        page = row["target_page"]
        if section_key not in _valid_section_keys(page):
            section_key = "__bottom__"
        opts = _parse_options(options) if row["input_type"] == "dropdown" else []
        if row["input_type"] == "dropdown" and not opts:
            return _msg("/admin/custom-fields", "Dropdown needs at least one option", page)
        conn.execute(
            'UPDATE "app_custom_fields" SET "label"=?,"section_key"=?,"options_json"=?,"required"=?,"sort_order"=?,"placeholder"=?,"default_value"=? WHERE "id"=?',
            (label.strip() or row["label"], section_key, json.dumps(opts), 1 if required else 0, sort_order, placeholder.strip(), default_value.strip(), field_id))
        log_audit(conn, "EDIT_CUSTOM_FIELD", "app_custom_fields", row["field_key"])
    return _msg("/admin/custom-fields", "Custom field updated", page)


@router.post("/admin/custom-fields/toggle/{field_id}")
def custom_fields_toggle(field_id: int):
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        row = conn.execute('SELECT "field_key","active","target_page" FROM "app_custom_fields" WHERE "id"=?', (field_id,)).fetchone()
        if not row:
            return _msg("/admin/custom-fields", "Field not found")
        conn.execute('UPDATE "app_custom_fields" SET "active"=? WHERE "id"=?', (0 if row["active"] else 1, field_id))
        log_audit(conn, "TOGGLE_CUSTOM_FIELD", "app_custom_fields", row["field_key"])
    return _msg("/admin/custom-fields", "Field hidden (data kept)" if row["active"] else "Field shown", row["target_page"])


@router.post("/admin/custom-fields/delete/{field_id}")
def custom_fields_delete(field_id: int):
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        row = conn.execute('SELECT "field_key","target_page" FROM "app_custom_fields" WHERE "id"=?', (field_id,)).fetchone()
        if not row:
            return _msg("/admin/custom-fields", "Field not found")
        conn.execute('DELETE FROM "app_custom_fields" WHERE "id"=?', (field_id,))
        log_audit(conn, "DEL_CUSTOM_FIELD", "app_custom_fields", row["field_key"])
    return _msg("/admin/custom-fields", "Custom field and its saved values deleted", row["target_page"])


# Built-in field metadata edit / toggle (never deletes legacy fields)
@router.post("/admin/builtin-fields/update/{builtin_id}")
def builtin_fields_update(builtin_id: int, label: str = Form(...), sort_order: int = Form(10),
                          placeholder: str = Form(""), default_value: str = Form("")):
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        row = conn.execute('SELECT * FROM "app_builtin_fields" WHERE "id"=?', (builtin_id,)).fetchone()
        if not row:
            return _msg("/admin/custom-fields", "Built-in field not found")
        conn.execute(
            'UPDATE "app_builtin_fields" SET "label"=?,"sort_order"=?,"placeholder"=?,"default_value"=? WHERE "id"=?',
            (label.strip() or row["label"], sort_order, placeholder.strip(), default_value.strip(), builtin_id))
        save_builtin_layout_to_json(conn)
        log_audit(conn, "EDIT_BUILTIN_FIELD", "app_builtin_fields", f"{row['target_page']}.{row['input_name']} (order={sort_order})")
        page = row["target_page"]
    return _msg("/admin/custom-fields", f"Updated '{label.strip()}' (order {sort_order}) and saved to builtin_layout.json", page)


@router.post("/admin/builtin-fields/sync-json")
def builtin_fields_sync_json(page: str = Form("workover")):
    target_page = page if page in PAGES else "workover"
    with db_session(write=True) as conn:
        cnt = sync_builtin_layout_from_json(conn)
        log_audit(conn, "SYNC_BUILTIN_JSON", "app_builtin_fields", f"synced {cnt} fields from builtin_layout.json")
    return _msg("/admin/custom-fields", f"Reloaded {cnt} built-in field settings from builtin_layout.json", target_page)


@router.post("/admin/builtin-fields/toggle/{builtin_id}")
def builtin_fields_toggle(builtin_id: int):
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        row = conn.execute('SELECT * FROM "app_builtin_fields" WHERE "id"=?', (builtin_id,)).fetchone()
        if not row:
            return _msg("/admin/custom-fields", "Built-in field not found")
        conn.execute('UPDATE "app_builtin_fields" SET "active"=? WHERE "id"=?', (0 if row["active"] else 1, builtin_id))
        save_builtin_layout_to_json(conn)
        log_audit(conn, "TOGGLE_BUILTIN_FIELD", "app_builtin_fields", f"{row['target_page']}.{row['input_name']}")
    return _msg("/admin/custom-fields", "Built-in field hidden" if row["active"] else "Built-in field shown", row["target_page"])


# ---------------------------------------------------------------- JSON API (page layout + values)
@router.get("/api/page-layout")
def api_page_layout(page: str = Query(...), well: str = "", run_date: str = ""):
    if page not in PAGES:
        return JSONResponse({"ok": False, "error": "Unknown page"}, status_code=400)
    rd = _to_iso_date_str(run_date) or ""
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        builtins = [dict(r) for r in conn.execute(
            'SELECT * FROM "app_builtin_fields" WHERE "target_page"=? ORDER BY "section_key","sort_order","id"',
            (page,)).fetchall()]
        customs = get_fields(conn, page, include_inactive=False)
        vals = {}
        if well.strip() and rd and customs:
            ids = [f["id"] for f in customs]
            q = ",".join("?" * len(ids))
            for r in conn.execute(
                f'SELECT "field_id","value" FROM "app_custom_values" WHERE "WellName"=? AND "WellRunDate"=? AND "field_id" IN ({q})',
                [well.strip(), rd] + ids).fetchall():
                vals[r["field_id"]] = r["value"]

    valid_keys = _valid_section_keys(page)
    sections_out = []
    for sec in _sections_for_page(page):
        sk = sec["key"]
        items = []
        for b in builtins:
            if b["section_key"] == sk:
                items.append({
                    "kind": "builtin", "name": b["input_name"], "label": b["label"],
                    "order": int(b["sort_order"] or 0), "active": bool(b["active"]),
                    "placeholder": b.get("placeholder") or "",
                    "default_value": b.get("default_value") or "",
                })
        for c in customs:
            c_sec = c["section_key"] if c["section_key"] in valid_keys else "__bottom__"
            if c_sec == sk:
                items.append({
                    "kind": "custom", "key": c["field_key"], "label": c["label"],
                    "type": c["input_type"], "options": c["options"],
                    "required": bool(c["required"]), "order": int(c["sort_order"] or 0),
                    "value": vals.get(c["id"], ""),
                    "placeholder": c.get("placeholder") or "",
                    "default_value": c.get("default_value") or "",
                })
        items.sort(key=lambda x: (x["order"], 0 if x["kind"] == "builtin" else 1))
        sections_out.append({"key": sk, "title": sec["title"], "items": items})

    return {"ok": True, "page": page, "run_date": rd, "sections": sections_out}


@router.get("/api/custom-fields")
def api_custom_fields(page: str = Query(...), well: str = "", run_date: str = ""):
    if page not in PAGES:
        return JSONResponse({"ok": False, "error": "Unknown page"}, status_code=400)
    rd = _to_iso_date_str(run_date) or ""
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        fields = get_fields(conn, page)
        vals = {}
        if well.strip() and rd and fields:
            ids = [f["id"] for f in fields]
            q = ",".join("?" * len(ids))
            for r in conn.execute(
                f'SELECT "field_id","value" FROM "app_custom_values" WHERE "WellName"=? AND "WellRunDate"=? AND "field_id" IN ({q})',
                [well.strip(), rd] + ids).fetchall():
                vals[r["field_id"]] = r["value"]
    return {"ok": True, "run_date": rd, "fields": [
        {"key": f["field_key"], "label": f["label"], "type": f["input_type"], "options": f["options"],
         "required": bool(f["required"]), "section_key": f["section_key"],
         "placeholder": f.get("placeholder") or "", "default_value": f.get("default_value") or "",
         "value": vals.get(f["id"], "")} for f in fields]}


class CustomValuesIn(BaseModel):
    page: str
    well: str
    run_date: str
    values: dict


@router.post("/api/custom-values")
def api_save_custom_values(body: CustomValuesIn):
    well = body.well.strip()
    rd = _to_iso_date_str(body.run_date) or ""
    if body.page not in PAGES or not well or not _ISO.match(rd):
        return JSONResponse({"ok": False, "error": "Select a Well Name and a valid Run Date first."}, status_code=422)
    errors, clean = {}, {}
    with db_session(write=True) as conn:
        _ensure_layout_schema(conn)
        fields = {f["field_key"]: f for f in get_fields(conn, body.page)}
        for key, f in fields.items():
            if key not in body.values:
                continue
            v, err = _validate(f, body.values[key])
            if err:
                errors[key] = err
            else:
                clean[key] = v
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=422)
        for key, v in clean.items():
            fid = fields[key]["id"]
            if v == "":
                conn.execute('DELETE FROM "app_custom_values" WHERE "field_id"=? AND "WellName"=? AND "WellRunDate"=?', (fid, well, rd))
            else:
                conn.execute(
                    'INSERT INTO "app_custom_values" ("field_id","WellName","WellRunDate","value","updated_by") VALUES (?,?,?,?,?) '
                    'ON CONFLICT("field_id","WellName","WellRunDate") DO UPDATE SET "value"=excluded."value","updated_by"=excluded."updated_by","updated_at"=CURRENT_TIMESTAMP',
                    (fid, well, rd, v, get_current_username()))
        log_audit(conn, "SAVE_CUSTOM_VALUES", "app_custom_values", f"{well} {rd} [{body.page}] x{len(clean)}")
    return {"ok": True, "saved": len(clean)}