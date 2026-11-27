"""Styled OpenPyXL Excel (.xlsx) generator for Wellbore Sketch, Workover, and Pump Tear Down reports."""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

NAVY = PatternFill(start_color="1B365D", end_color="1B365D", fill_type="solid")
GREEN = PatternFill(start_color="A9D08E", end_color="A9D08E", fill_type="solid")
PEACH = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
GRAY = PatternFill(start_color="EBF1F6", end_color="EBF1F6", fill_type="solid")
ZEBRA = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

F_TITLE = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
F_SEC = Font(name="Calibri", size=11, bold=True, color="1A202C")
F_HDR = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
F_BOLD = Font(name="Calibri", size=10, bold=True, color="1A202C")
F_CELL = Font(name="Calibri", size=10, color="1A202C")

BDR = Border(
    left=Side(style="thin", color="94A3B8"), right=Side(style="thin", color="94A3B8"),
    top=Side(style="thin", color="94A3B8"), bottom=Side(style="thin", color="94A3B8"),
)


def _fit(ws):
    for col in ws.columns:
        w = 10
        for c in col:
            w = max(w, min(len(str(c.value or "")), 48))
        ws.column_dimensions[get_column_letter(col[0].column)].width = w + 3


def _banner(ws, title, subtitle, cols=8):
    ec = get_column_letter(max(cols, 6))
    ws.merge_cells(f"A1:{ec}1")
    ws["A1"] = title
    ws["A1"].font, ws["A1"].fill = F_TITLE, NAVY
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    ws.merge_cells(f"A2:{ec}2")
    ws["A2"] = subtitle
    ws["A2"].font, ws["A2"].fill = F_BOLD, PEACH
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20


def _sec_hdr(ws, row, text, cols=6, fill=PEACH):
    ec = get_column_letter(cols)
    ws.merge_cells(f"A{row}:{ec}{row}")
    ws.cell(row=row, column=1, value=text).font = F_SEC
    ws.cell(row=row, column=1).fill = fill
    ws.cell(row=row, column=1).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 20
    return row + 1


def _kv(ws, r, pairs, per_row=2):
    for i in range(0, len(pairs), per_row):
        col = 1
        for k, v in pairs[i:i + per_row]:
            ck = ws.cell(row=r, column=col, value=k)
            ck.font, ck.fill, ck.border = F_BOLD, GRAY, BDR
            cv = ws.cell(row=r, column=col + 1, value=v if v is not None else "")
            cv.font, cv.border = F_CELL, BDR
            col += 2
        r += 1
    return r + 1


def _tbl(ws, r, headers, rows):
    for idx, h in enumerate(headers, 1):
        c = ws.cell(row=r, column=idx, value=h)
        c.font, c.fill, c.border = F_HDR, NAVY, BDR
        c.alignment = Alignment(horizontal="center", vertical="center")
    r += 1
    for i, rv in enumerate(rows):
        for idx, v in enumerate(rv, 1):
            c = ws.cell(row=r, column=idx, value=v if v is not None else "")
            c.font, c.border = F_CELL, BDR
            if i % 2 == 1:
                c.fill = ZEBRA
        r += 1
    return r + 1


def build_report_workbook(report_type: str, ctx: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    well, rdate = ctx.get("well_name", ""), ctx.get("run_date", "")

    if report_type == "rptwellboresketch":
        s = ctx["summary"]
        tm, p, rm = s["tbg_master"], s["pump"], s["rod_meta"]
        _banner(ws, f"WELLBORE SKETCH — {well}", f"Well Run Date: {rdate}   |   Completion: {s['completion_type']}", 6)
        r = _sec_hdr(ws, 4, "Well Data & Pump Data", 6, GREEN)
        r = _kv(ws, r, [
            ("RIH w/Rods Rig", rm.get("RigName")), ("Pump SN", p.get("PumpSN")), ("SV", p.get("SV")),
            ("RIH w/TBG Rig", tm.get("RIHtbgrig")), ("Pump Design", p.get("Pumpdesignation")), ("DSV", p.get("DSV")),
            ("Job Type", rm.get("JobType")), ("Clearance", p.get("PlungerClearance")), ("TV", p.get("TV")),
            ("ORKB (ft)", tm.get("ORKB")), ("Vendor", p.get("Vendor")), ("DTV", p.get("DTV")),
            ("Rod Rotator", rm.get("RodRotator")), ("Plug Seat", p.get("PlugSeat")), ("O.Plunger", p.get("Openplunger")),
        ], per_row=3)
        r = _sec_hdr(ws, r, "Well String Data (Tubing & Rod String)", 5, GREEN)
        rows = [[x.get("itemtype"), x.get("itemno"), x.get("itemlength"), x.get("itemdescrip") or x.get("itemsize"), x.get("itemcond")] for x in s["tbg_rows"]]
        rows.append(["---", "---", "---", "--- ROD STRING ---", "---"])
        rows.extend([[x.get("itemtype"), x.get("itemno"), x.get("itemlength"), x.get("itemsize"), x.get("itemcond")] for x in s["rod_rows"]])
        if p.get("Pumpdesignation"):
            rows.append(["PUMP", 1, p.get("PumpLength"), p.get("Pumpdesignation"), p.get("PumpCOND")])
        rows.extend([[x.get("itemtype"), x.get("itemno"), x.get("itemlength"), x.get("itemsize"), x.get("itemcond")] for x in s.get("intake_rows", [])])
        r = _tbl(ws, r, ["Item Type", "No.", "Length (ft)", "Description", "Cond."], rows)
        r = _sec_hdr(ws, r, "TAC Set, Rig Weights, Comments & Depths (ORKB)", 6, GREEN)
        r = _kv(ws, r, [
            ("TAC Set (in)", tm.get("TBGStretch")), ("TAC Overpull (LBS)", tm.get("TACOverPull")), ("TAC Depth (ORKB)", tm.get("TACDepth")),
            ("Weight Ind. Start", tm.get("WeightIndicator(Start)")), ("Weight Ind. End", tm.get("WeightIndicator(End)")), ("SN Depth (ORKB)", tm.get("SNDepth")),
            ("T.Block wt (LBS)", tm.get("tbgrihrigblock")), ("X under collar (in)", tm.get("Distanceundercollar(X)")), ("WLEG / B.PLUG Depth", tm.get("WLEGDepth") or tm.get("BULLPLUGDepth")),
            ("KTH Flange Size", tm.get("KTHFlangeSize")), ("KTH Flange Type", tm.get("KTHFlangeType")), ("Top / Bot Perf", f"{tm.get('TopPerfDepth') or ''} / {tm.get('BotPerfDepth') or ''}"),
            ("Changed", f"{rm.get('Changed1') or ''} | {rm.get('Changed2') or ''}".strip(" |")), ("Report Done By", rm.get("Supervisor")), ("PBTD (ORKB)", tm.get("PBTD")),
            ("SR Team", rm.get("Foreman")), ("Pump Notes", p.get("notes")), ("General Comments", tm.get("Generalcomments")),
        ], per_row=3)

    elif report_type == "rptpumpreport":
        p, wo, tm = ctx["pump"], ctx["workover"], ctx["tbg_master"]
        _banner(ws, "PUMP TEAR DOWN AND INSPECTION REPORT", f"Well: {well}  |  Run Date: {rdate}  |  Pump SN: {p.get('PumpSN') or ''}", 6)
        r = _kv(ws, 4, [
            ("Well Name", well), ("Pump SN", p.get("PumpSN")), ("SN Depth", tm.get("SNDepth")),
            ("Well Run Date", rdate), ("API Design", p.get("Pumpdesignation")), ("Well Speed", wo.get("Wellspeed")),
            ("Report Date", p.get("pumpreportdate")), ("Running Cond", p.get("PumpCOND")), ("SL", wo.get("SL")),
            ("Well Type", p.get("welltype")), ("Inspection Cond.", p.get("pumpinspectioncond")), ("Start Date", p.get("StartDate")),
            ("W/Over Reason", wo.get("W/Oreason")), ("Plunger Clearance", p.get("PlungerClearance")), ("Failure Date", p.get("FailureDate")),
            ("Well Failure Reason", wo.get("Wellfailurereason")), ("Vendor", p.get("Vendor")), ("Pump Lifetime", p.get("PumpLifetime")),
        ], per_row=3)
        r = _sec_hdr(ws, r, "Barrel Assembly", 5, NAVY)
        ws.cell(row=r-1, column=1).font = F_HDR
        r = _tbl(ws, r, ["Pump Part", "Cond.", "Defect", "Detailed Description / Apparent Failure", "Trim"], [
            ["Barrel (Heavy wall)", p.get("barrelcond"), p.get("barreldefect"), p.get("barrelapparentfailure"), ""],
            ["Valve Rod Guide", p.get("rodguidecond"), p.get("rodguidedefect"), p.get("rodguideapparentfailure"), ""],
            ["Barrel Conn./Conv. Kit", p.get("barrelconnectorcond"), p.get("barrelconnectordefect"), p.get("barrelconnectorapparentfailure"), ""],
            ["Top Extension", p.get("topextcond"), p.get("topextdefect"), p.get("topextapparentfailure"), ""],
            ["Bottom Extension", p.get("botextcond"), p.get("botextdefect"), p.get("botextapparentfailure"), ""],
            ["SV Closed Cage", p.get("SVcond"), p.get("SVdefect"), p.get("SVapparentfailure"), p.get("SV")],
            ["SV Ball / Seat / Test", f"{p.get('SVballcond') or ''} / {p.get('SVseatcond') or ''}", p.get("SVballseattest"), p.get("SVballseatcomment"), ""],
            ["DSV Closed Cage", p.get("DSVcond"), p.get("DSVdefect"), p.get("DSVapparentfailure"), p.get("DSV")],
            ["DSV Ball / Seat / Test", f"{p.get('DSVballcond') or ''} / {p.get('DSVseatcond') or ''}", p.get("DSVballseattest"), p.get("DSVballseatcomment"), ""],
            ["Coupling / Plug Seat", p.get("coup/pseatcond"), p.get("coup/pseatdefect"), p.get("coup/pseatapparentfailure"), ""],
        ])
        r = _sec_hdr(ws, r, "Plunger Assembly", 5, NAVY)
        ws.cell(row=r-1, column=1).font = F_HDR
        r = _tbl(ws, r, ["Pump Part", "Cond.", "Defect", "Detailed Description / Apparent Failure", "Trim"], [
            ["Plunger", p.get("plungercond"), p.get("plungerdefect"), p.get("plungerapparentfailure"), ""],
            ["Valve Rod", p.get("vrodcond"), p.get("vroddefect"), p.get("vrodapparentfailure"), ""],
            ["Valve Rod Bushing", p.get("vrodbushingcond"), p.get("vrodbushingdefect"), p.get("vrodbushingapparentfailure"), ""],
            ["Valve Rod Bushing Nut", p.get("vrodnutcond"), p.get("vrodnutdefect"), p.get("vrodnutapparentfailure"), ""],
            ["Open Plunger Cage", p.get("oplungercond"), p.get("oplungerdefect"), p.get("oplungerapparentfailure"), p.get("Openplunger")],
            ["TV Closed Cage", p.get("TVcond"), p.get("TVdefect"), p.get("TVapparentfailure"), p.get("TV")],
            ["TV Ball / Seat / Test", f"{p.get('TVballcond') or ''} / {p.get('TVseatcond') or ''}", p.get("TVballseattest"), p.get("TVballseatcomment"), ""],
            ["DTV Closed Cage", p.get("DTVcond"), p.get("DTVdefect"), p.get("DTVapparentfailure"), p.get("DTV")],
            ["DTV Ball / Seat / Test", f"{p.get('DTVballcond') or ''} / {p.get('DTVseatcond') or ''}", p.get("DTVballseattest"), p.get("DTVballseatcomment"), ""],
            ["TV Plug Seat", p.get("TVpseatcond"), p.get("TVpseatdefect"), p.get("TVpseatapparentfailure"), p.get("PlugSeat")],
        ])
        r = _sec_hdr(ws, r, "Hold Down Assembly", 5, NAVY)
        ws.cell(row=r-1, column=1).font = F_HDR
        r = _tbl(ws, r, ["Pump Part", "Cond.", "Defect", "Detailed Description / Apparent Failure", "Notes"], [
            ["Bushing", p.get("hdownbushcond"), p.get("hdownbushdefect"), p.get("hdownbushapparentfailure"), ""],
            ["Mandrel", p.get("mandrelcond"), p.get("mandreldefect"), p.get("mandrelapparentfailure"), ""],
            ["Sleeve", p.get("sleevecond"), p.get("sleevedefect"), p.get("sleeveapparentfailure"), ""],
            ["Seating Ring", p.get("hdownringcond"), p.get("hdownringdefect"), p.get("hdownringapparentfailure"), ""],
            ["Pulled S.Screen", "", "", wo.get("Pulledsandscreen"), ""],
        ])
        _kv(ws, r, [("General Comments", p.get("comments")), ("Pictures Location", p.get("locationonpc")), ("Prepared By", p.get("preparedby")), ("Inspected By", p.get("inspectby"))], per_row=1)

    else:
        wo = ctx["workover"]
        wm = ctx.get("wear_meta") or {}
        _banner(ws, "WORK OVER REPORT", f"Well: {well}  |  Run Date: {rdate}  |  Well Lifetime: {wo.get('WellLifeTime') or '-'} Days", 8)
        r = _kv(ws, 4, [
            ("S/D Date", wo.get("SDDate")), ("POOH W/Rods Rig", wo.get("RIGNAMErods")), ("POOH W/TBG Rig", wo.get("rignametbg")),
            ("Rig Date", wo.get("WOverDate")), ("Rods T.Block wt (LBS)", wo.get("T/BlockWTrod")), ("TBG T.Block wt (LBS)", wo.get("T/BlockWTtbg")),
        ], per_row=3)
        r = _sec_hdr(ws, r, "Rod String Data & TAC Unset Data", 6, PEACH)
        r = _kv(ws, r, [
            ("Cal. normal wt (LBS)", wo.get("Calrodstringwt")), ("Matched ?", wo.get("Matched")),
            ("Cal. Over Pull (LBS)", wo.get("CalOverPull")), ("Unset Pump ?", wo.get("PumpUnset")),
            ("Act. Over Pull (LBS)", wo.get("ActOverPull")), ("Back-Off ?", wo.get("BackOff")),
            ("Unset TAC ?", wo.get("TACunsettrial")), ("TAC Status", wo.get("TACstatus")),
        ], per_row=2)
        r = _sec_hdr(ws, r, "Rig Situation & Well-Failure Reason", 6, PEACH)
        r = _kv(ws, r, [
            ("Well Status", wo.get("WellStatus")), ("W/Over Reason", wo.get("W/Oreason")),
            ("1st P/T result", wo.get("1stP/Tresult")), ("Pressure Drop", f"{wo.get('Pressuredroppedfrom') or ''} to {wo.get('Pressuredroppedto') or ''} psi in {wo.get('within/minutes') or ''} min"),
            ("Norm Readings", f"{wo.get('Normreadingsfrom') or ''} to {wo.get('Normreadingsto') or ''} Msv ({wo.get('Normstatus') or ''})"), ("Pulled Sand Screen", wo.get("Pulledsandscreen")),
            ("Pulled DHP test", wo.get("PulledDHPtest")), ("MSV P/T & Result", f"{wo.get('MSVtest') or ''} — {wo.get('MSVtestresult') or ''}"),
            ("Well-Failure Reason", wo.get("Wellfailurereason")), ("Pump Failure Type", wo.get("pumpfailure type")),
            ("PSN Ring Test", wo.get("PSNringtest")), ("General Comments", wo.get("Generalnotes")),
        ], per_row=2)
        if report_type != "rptwoverwowear":
            r = _sec_hdr(ws, r, f"Wear Comments (Scale: {wo.get('Scalepresence') or '-'} | Pitting: {wm.get('Pitting') or '-'})", 11, PEACH)
            headers = ["Rod Type", "Grade", "No", "F/D", "T/D", "Coupling", "Shoulder", "Neck", "Body", "Guides", "Wear Rate"]
            wrows = [[x.get(k) for k in ("Rodtype","Rodgrade","Noofrods","FDepth","TDepth","Coupling","Shoulder","Neck","Body","Guides","Wearrate")] for x in (ctx.get("wear_rows") or [])]
            r = _tbl(ws, r, headers, wrows)
            _kv(ws, r, [("Wear Comments", wm.get("General Comments")), ("Report Done By", wo.get("Supervisor")), ("SR Team", wo.get("Foreman"))], per_row=1)

    _fit(ws)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
