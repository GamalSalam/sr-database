"""Engineering calculations & unified Wellbore Sketch data builder."""

ROD_WEIGHT_LB_PER_FT = {
    "5/8": 1.135, "3/4": 1.634, "7/8": 2.224, "1": 2.904,
    "1-1/8": 3.676, "1-1/4": 4.200, "1-1/2": 6.008, "1-5/8": 7.050, "1-3/4": 8.180,
}


def _sf(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        s = str(val).replace(",", "").replace("ft", "").replace("lbs", "").replace('"', "").strip()
        return float(s) if s else default
    except Exception:
        return default


def _rod_w(size_str: str) -> float:
    s = (size_str or "").lower()
    for k in ["1-3/4", "1-5/8", "1-1/2", "1-1/4", "1-1/8", "7/8", "3/4", "5/8"]:
        if k in s:
            return ROD_WEIGHT_LB_PER_FT[k]
    return ROD_WEIGHT_LB_PER_FT["1"] if "1" in s else 2.224


# ---------------------------------------------------------------------------
# Rod string source selection (new tables vs. legacy Access wide tables)
# ---------------------------------------------------------------------------
_PONY_FIELDS = [  # (column prefix, item type) in SURFACE -> DOWN order (Access stores them bottom-up)
    ("2ftPonyRod", "Pony Rod 2'"), ("PolishedRod", "Polished Rod"),
    ("PonyRod2ft", "Pony Rod 2'"), ("PonyRod4ft", "Pony Rod 4'"),
    ("PonyRod6ft", "Pony Rod 6'"), ("PonyRod8ft", "Pony Rod 8'"),
]


def _is_intake_item(r: dict) -> bool:
    t = str(r.get("itemtype") or "").lower()
    return "sand screen" in t or "dip tube" in t or "intake" in t


def _has_detail(r: dict) -> bool:
    return bool(str(r.get("itemno") or "").strip() or _sf(r.get("itemlength")) > 0 or str(r.get("itemsize") or "").strip())


def _legacy_item(order, itype, no, length, size, cond) -> dict:
    return {"intervalno": str(order), "itemtype": itype, "itemno": no, "itemlength": length,
            "itemsize": size, "itemcond": cond, "_src": "legacy"}


def _clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return s


def _legacy_rod_string(conn, well_name, run_date, rod_meta):
    """Rebuild the full string (surface -> down) from tblrodstringdata1 (+ pony/polished/Rod11-15 in tblrodstringdata2).
    Returns (rod_rows, intake_rows). Intake = items BELOW the pump (dip tube, sand screen)."""
    q = 'SELECT * FROM "tblrodstringdata1" WHERE "WellName"=?' + (' AND "WellRunDate"=?' if run_date else "")
    row = conn.execute(q + ' ORDER BY "WellRunDate" DESC, "_rowid_pk" DESC LIMIT 1',
                       [well_name] + ([run_date] if run_date else [])).fetchone()
    d1 = dict(row) if row else {}
    d2 = rod_meta or {}
    if not d1 and not d2:
        return [], []

    rods, order = [], 0

    def used(no, length):
        return _sf(no) > 0 or _sf(length) > 0

    def add(itype, no, length, size, cond):
        nonlocal order
        order += 1
        rods.append(_legacy_item(order, itype, _clean(no), _clean(length), _clean(size), _clean(cond)))

    for pre, label in _PONY_FIELDS:                       # ponies + polished rod (data2)
        if pre == "PolishedRod":
            no, ln, sz, cd = d2.get("PolishedRodNo"), d2.get("PolishedRodLength"), d2.get("PolishedRodDescrip"), d2.get("PolishedRodCond")
        else:
            no, ln, sz, cd = d2.get(f"{pre}No"), d2.get(f"{pre}Length"), d2.get(f"{pre}Grade"), d2.get(f"{pre}Cond")
        if used(no, ln):
            add(label, no, ln, sz, cd)

    for n in range(15, 0, -1):                            # Rod15..Rod11 (data2) then Rod10..Rod1 (data1): top -> bottom
        src = d2 if n >= 11 else d1
        t, no, ln = src.get(f"Rod{n}Type"), src.get(f"Rod{n}No"), src.get(f"Rod{n}Length")
        if str(t or "").strip() and used(no, ln):
            add(t, no, ln, src.get(f"Rod{n}Grade"), src.get(f"Rod{n}Cond"))

    if used(d1.get("StabBarNo"), d1.get("StabBarLength")):  # stab bar sits just above the pump
        add(d1.get("StabBarSize") or "Stab Bar", d1.get("StabBarNo"), d1.get("StabBarLength"),
            d1.get("StabBarDescription"), d1.get("StabBarCond"))

    intake, o2 = [], 0
    for label, pre in (("Dip Tube", "DipTube"), ("Sand Screen", "SandScreen")):   # below the pump, sand screen lowest
        if used(d1.get(f"{pre}No"), d1.get(f"{pre}Length")):
            o2 += 1
            intake.append(_legacy_item(o2, label, _clean(d1.get(f"{pre}No")), _clean(d1.get(f"{pre}Length")),
                                       _clean(d1.get(f"{pre}Size")), _clean(d1.get(f"{pre}Cond"))))
    return rods, intake


def calculate_well_summary(conn, well_name: str, run_date: str = None) -> dict:
    params = [well_name]
    dclause = ' AND "WellRunDate" = ?' if run_date else ""
    if run_date:
        params.append(run_date)

    tbg_rows = [dict(r) for r in conn.execute(
        f'SELECT * FROM "tblTBGdatanew" WHERE "WellName"=?{dclause} ORDER BY CAST("intervalno" AS INTEGER), "id"', params
    ).fetchall()]
    tbg_master = conn.execute(
        f'SELECT * FROM "tblTBGdata" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params
    ).fetchone()
    tbg_master = dict(tbg_master) if tbg_master else {}

    rod_rows = [dict(r) for r in conn.execute(
        f'SELECT * FROM "tblrodsdatanew" WHERE "WellName"=?{dclause} ORDER BY CAST("intervalno" AS INTEGER), "id"', params
    ).fetchall()]
    rod_meta = conn.execute(
        f'SELECT * FROM "tblrodstringdata2" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params
    ).fetchone()
    rod_meta = dict(rod_meta) if rod_meta else {}

    pump = conn.execute(
        f'SELECT * FROM "tblpumpdata" WHERE "WellName"=?{dclause} ORDER BY "WellRunDate" DESC LIMIT 1', params
    ).fetchone()
    pump = dict(pump) if pump else {}

    wover = conn.execute(
        f'SELECT * FROM "tblwoverdata" WHERE "WellName"=?{dclause} ORDER BY "WOverDate" DESC LIMIT 1', params
    ).fetchone()
    wover = dict(wover) if wover else {}

    effective_run_date = (
        run_date
        or tbg_master.get("WellRunDate")
        or pump.get("WellRunDate")
        or rod_meta.get("WellRunDate")
        or (rod_rows[0].get("WellRunDate") if rod_rows else None)
    )

    # ---- choose the rod string source -------------------------------------------------
    # New app entry (tblrodsdatanew with real details + Polished Rod) wins.
    # Imported Access runs in tblrodsdatanew often hold only rod tapers (or only item types),
    # so they are rebuilt from tblrodstringdata1/2 to include ponies, polished rod, stab bar,
    # and below-pump intake items (dip tube / sand screen).
    new_intake = [r for r in rod_rows if _is_intake_item(r) and _has_detail(r)]
    new_main = [r for r in rod_rows if not _is_intake_item(r)]
    has_new_details = any(_has_detail(r) for r in new_main)
    has_new_surface = any("polished" in str(r.get("itemtype") or "").lower() for r in new_main)

    rod_source = "new"
    l_rods, legacy_intake = _legacy_rod_string(conn, well_name, effective_run_date, rod_meta)

    if (not has_new_details or not has_new_surface) and (l_rods or legacy_intake):
        rod_rows, rod_source = l_rods, "legacy"
    else:
        rod_rows = new_main

    try:   # items below the pump entered on the Rod String page
        app_intake = [dict(r) for r in conn.execute(
            f'SELECT * FROM "app_pump_intake_items" WHERE "WellName"=?{dclause} ORDER BY CAST("intervalno" AS INTEGER), "id"', params
        ).fetchall()]
    except Exception:
        app_intake = []
    intake_rows = (app_intake + new_intake) or legacy_intake

    total_rods_count = 0
    total_rod_length = 0.0
    total_rod_weight_air = 0.0
    count_34 = count_78 = count_1 = count_kbar = 0
    pony_length = 0.0

    for r in rod_rows:
        n = int(_sf(r.get("itemno"), 0))
        length = _sf(r.get("itemlength"), 0.0)
        itype = str(r.get("itemtype") or "")
        size = str(r.get("itemsize") or "")
        comb = (itype + " " + size).lower()
        if "sand screen" in comb or "dip tube" in comb:
            continue
        total_rods_count += n
        total_rod_length += length
        total_rod_weight_air += length * _rod_w(comb)
        if "pony" in comb:
            pony_length += length
        elif "guided" in comb or "k-bar" in comb or "sinker" in comb or "stab" in comb:
            count_kbar += n
        elif "3/4" in comb:
            count_34 += n
        elif "7/8" in comb:
            count_78 += n
        elif "1" in comb and "polished" not in comb:
            count_1 += n

    orkb = _sf(tbg_master.get("ORKB"), 22.0)
    tbg_total_joints = sum(int(_sf(x.get("itemno"), 0)) for x in tbg_rows)
    tbg_total_length = round(sum(_sf(x.get("itemlength"), 0.0) for x in tbg_rows), 2)
    tbg_eod = round(orkb + tbg_total_length, 2)

    return {
        "well_name": well_name,
        "run_date": effective_run_date or "",
        "completion_type": tbg_master.get("CompletionType") or "WLEG",
        "orkb": orkb,
        "tbg_total_joints": tbg_total_joints,
        "tbg_total_length": tbg_total_length,
        "tbg_eod": tbg_eod,
        "rod_total_count": total_rods_count,
        "rod_total_length": round(total_rod_length, 1),
        "rod_weight_air_lbs": round(total_rod_weight_air, 1),
        "rod_weight_buoyed_lbs": round(total_rod_weight_air * 0.872, 1),
        "count_1": count_1, "count_78": count_78, "count_34": count_34,
        "count_kbar": count_kbar, "pony_length": round(pony_length, 1),
        "sn_depth": tbg_master.get("SNDepth") or "",
        "tac_depth": tbg_master.get("TACDepth") or "",
        "wleg_depth": tbg_master.get("WLEGDepth") or "",
        "bplug_depth": tbg_master.get("BULLPLUGDepth") or "",
        "pkr_depth": tbg_master.get("HallPHLHYDPKRDepth") or "",
        "top_perf": tbg_master.get("TopPerfDepth") or "",
        "bot_perf": tbg_master.get("BotPerfDepth") or "",
        "pbtd": tbg_master.get("PBTD") or "",
        "pump": pump, "wover": wover, "tbg_master": tbg_master, "rod_meta": rod_meta,
        "tbg_rows": tbg_rows, "rod_rows": rod_rows,
        "intake_rows": intake_rows, "rod_source": rod_source,
    }