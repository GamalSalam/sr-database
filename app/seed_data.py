"""Seeds default Admin-configurable dropdown lists + ISIS-05 sample data."""

DROPDOWN_FIELD_REGISTRY = {
    "wo_rig_name": "Workover — Rig Name (Rods / TBG)",
    "wo_matched": "Workover — Matched?",
    "wo_yes_no": "Workover — Yes / No Fields",
    "wo_wear_comments": "Workover — Wear Comments Summary",
    "wo_scale_presence": "Workover — Scale Presence",
    "wo_well_status": "Workover — Well Status",
    "wo_reason": "Workover — W/O Reason",
    "wo_first_pt": "Workover — 1st P/T Result",
    "wo_norm_status": "Workover — Norm Status",
    "wo_sand_screen": "Workover — Pulled Sand Screen",
    "wo_dhp_test": "Workover — Pulled DHP Test",
    "wo_msv_result": "Workover — MSV P/T Result",
    "wo_psn_ring": "Workover — PSN Ring Test",
    "wo_failure_reason": "Workover — Well-Failure Reason",
    "wo_pump_failure": "Workover — Pump Failure Type",
    "wo_tbg_failure": "Workover — TBG Failure Type",
    "wo_rod_failure": "Workover — Rod Failure Type",
    "wo_parted_size": "Workover — Parted Rod Size",
    "wo_parted_grade": "Workover — Parted Rod Grade",
    "wo_rod_rotator": "Workover — Rod Rotator Status",
    "wo_kth_slips": "Workover — KTH Slips Status",
    "wo_polished_rod": "Workover — Polished Rod Status",
    "wo_report_by": "Workover — Report Done By",
    "wear_rod_type": "Rod Wear — Rod Type",
    "wear_rod_grade": "Rod Wear — Rod Grade",
    "wear_severity": "Rod Wear — Severity (Coupling/Shoulder/Neck/Body/Guides/Rate)",
    "tbg_item_type": "Tubing — Item Type",
    "tbg_cond": "Tubing — Condition",
    "rod_item_type": "Rod String — Item Type",
    "rod_cond": "Rod String — Condition",
    "rod_intake_type": "Rod String — Pump Intake Item Type (below pump)",
    "pump_size": "Pump — Pump Size",
    "pump_type": "Pump — Pump Type",
    "pump_extension": "Pump — Extension",
    "pump_barrel_len": "Pump — Barrel Length (ft)",
    "pump_vendor": "Pump — Vendor",
    "pump_plug_seat": "Pump — Plug Seat",
    "pump_clearance": "Pump — Plunger Clearance",
    "pump_cond": "Pump — Pump Condition",
    "pump_cage_mat": "Pump — Valve / Cage Material (SV/DSV/TV/DTV/O.Plunger)",
    "td_well_type": "Tear Down — Well Type",
    "td_insp_cond": "Tear Down — Inspection Condition",
    "td_part_cond": "Tear Down — Part Condition",
    "td_defect": "Tear Down — Part Defect",
    "td_ball_seat": "Tear Down — Ball / Seat Condition",
    "td_test_result": "Tear Down — Ball / Seat Test Result",
    "td_prepared_by": "Tear Down — Prepared By",
    "td_inspected_by": "Tear Down — Inspected By",
}


def seed_dropdowns(conn):
    defaults = [
        ("wo_rig_name", ["EDC-31", "EDC-32", "EDC-45", "Sino-21", "Sino-24", "Rig-14"]),
        ("wo_matched", ["Yes", "No"]),
        ("wo_yes_no", ["Yes", "No"]),
        ("wo_wear_comments", ["Pulled rods had wear", "No wear observed", "Severe coupling wear", "Guide wear only"]),
        ("wo_scale_presence", ["No", "Yes", "Light", "Moderate", "Heavy"]),
        ("wo_well_status", ["S/D due to DHproblem", "S/D due to Rod Parted", "S/D due to TBG Leak", "Running - Scheduled W/O"]),
        ("wo_reason", ["Pump replacement", "Rod parted", "TBG leak", "Pump & Rod replacement", "Sand cleanout", "Conversion"]),
        ("wo_first_pt", ["There was a leakage", "Hold (No leakage)", "Not performed"]),
        ("wo_norm_status", ["Normal Readings", "Above Normal (NORM)", "Not performed"]),
        ("wo_sand_screen", ["Fully plugged with Sands", "Partially plugged", "Clean", "Not installed"]),
        ("wo_dhp_test", ["Not Working", "Working - Low discharge", "Working OK", "Stuck plunger", "Not tested"]),
        ("wo_msv_result", ["HOLD", "Leaking", "Not performed"]),
        ("wo_psn_ring", ["Not performed", "Passed", "Failed / Worn"]),
        ("wo_failure_reason", ["Pump Problem", "Rod Parted", "TBG Failure", "Rod Parted - Pump Problem - TBG Failure", "Sanded Up", "TAC Failure"]),
        ("wo_pump_failure", ["Waiting on pump inspection", "Barrel worn", "Plunger stuck", "Valve rod parted", "SV/TV leaking", "No pump failure"]),
        ("wo_tbg_failure", ["No Hole", "Hole in TBG body", "Collar leak", "Split joint", "Pin/Box thread damaged"]),
        ("wo_rod_failure", ["No Rod Failure", "Pin Break", "Body Break", "Coupling Split", "Unscrewed"]),
        ("wo_parted_size", ['1"', '7/8"', '3/4"', '1" Guided', '7/8" Guided', '1-1/2" Sinker']),
        ("wo_parted_grade", ["N-97", "D-78", "K", "KD", "C"]),
        ("wo_rod_rotator", ["N/A", "Working", "Not Working", "Replaced"]),
        ("wo_kth_slips", ["Good", "Worn", "Replaced", "N/A"]),
        ("wo_polished_rod", ["Still working", "Scored - Replaced", "Bent - Replaced", "Stuffing box repacked"]),
        ("wo_report_by", ["M.Fathy", "Osama", "A. Farouk", "Tamer Abdelnaby", "Ahmed Elwakel", "Mahmoud Soliman"]),
        ("wear_rod_type", ['1" Normal', '7/8" Normal', '3/4" Normal', '1" Guided', '7/8" Guided', '3/4" Guided']),
        ("wear_rod_grade", ["N-97", "D-78", "K", "KD"]),
        ("wear_severity", ["-", "Light", "Slight", "Moderate", "Severe"]),
        ("tbg_item_type", ["TBG Jnts", "TAC", "SN", "WLEG", "B.PLUG", "Perf. Jnt.", "X/Over", "Nipple", "Hall HYD PKR", "X/O or Pup jnt"]),
        ("tbg_cond", ["A", "B (White Band)", "B", "New"]),
        ("rod_item_type", ["Pony Rod 2'", "Polished Rod", "Pony Rod 4'", "Pony Rod 6'", "Pony Rod 8'", '1" Normal', '7/8" Normal', '3/4" Normal', '1" Guided', '7/8" Guided', "Stab Bar 3.5", "Sand Screen", "Dip Tube"]),
        ("rod_cond", ["A", "B", "A + B", "New"]),
        ("rod_intake_type", ["Sand Screen", "Dip Tube", "Gas Separator", "Mud Anchor"]),
        ("pump_size", ["1.25", "1.5", "1.75", "2.0", "2.25", "2.5", "2.75"]),
        ("pump_type", ["Bottom", "Top"]),
        ("pump_extension", ["0,0", "2,0", "2,2", "2,3", "3,2", "3,3", "4,2"]),
        ("pump_barrel_len", ["12", "16", "18", "20", "22", "24", "26", "28", "30"]),
        ("pump_vendor", ["HF", "Dover", "Weatherford", "Lufkin", "ChampionX", "SLB"]),
        ("pump_plug_seat", ["Normal (A)", "Zero Monel (A)", "Zero SS (A)", "Monel (A)", "SS (A)"]),
        ("pump_clearance", ["-0.003", "-0.004", "-0.005", "-0.006", "-0.007", "-0.008"]),
        ("pump_cond", ["A", "B"]),
        ("pump_cage_mat", ["SS (A)", "Monel (A)", "Hardened (A)", "N/A"]),
        ("td_well_type", ["Vertical", "Deviated", "Horizontal"]),
        ("td_insp_cond", ["A", "B", "Scrap"]),
        ("td_part_cond", ["Fair", "Good", "Scrap", "N/A"]),
        ("td_defect", ["Wear", "Parted", "Thread galling", "Corrosion", "Scale", "Cracked", "Bent", "Scored", "Sand cut"]),
        ("td_ball_seat", ["OK", "Dull", "Cracked", "Pitted", "Scrap", "N/A"]),
        ("td_test_result", ["Hold", "Leak", "Not performed"]),
        ("td_prepared_by", ["Osama", "M.Fathy", "A. Farouk", "Tamer Abdelnaby"]),
        ("td_inspected_by", ["M.Abdelhay", "Mahmoud Soliman", "Ahmed Elwakel"]),
    ]
    existing_keys = {
        r[0] for r in conn.execute('SELECT DISTINCT "field_key" FROM "app_dropdown_options"').fetchall()
    }
    rows = []
    for fkey, opts in defaults:
        if fkey in existing_keys:
            continue
        flabel = DROPDOWN_FIELD_REGISTRY.get(fkey, fkey)
        for idx, opt in enumerate(opts, 1):
            rows.append((fkey, flabel, opt, idx * 10))
    if rows:
        conn.executemany(
            'INSERT OR IGNORE INTO "app_dropdown_options" ("field_key","field_label","option_value","sort_order") VALUES (?,?,?,?)',
            rows,
        )


def seed_database(conn):
    rod_grades = [
        ('1" Normal', 'N-97', '7', 'w/mandrel 3', 'Stab Bar 3.5', '4ft X 1in (7/8in pin)', '1"', 'N-97'),
        ('1" Normal', 'D-78', '5.5', 'w/mandrel 2-7/8', 'Stab Bar 3.5', '4ft X 1in (7/8in pin)', '1"', 'D-78'),
        ('1" Guided', 'N-97', None, None, None, None, '1" Guided', 'N-97'),
        ('1" Guided', 'D-78', None, None, None, None, '1" Guided', 'D-78'),
        ('7/8" Normal', 'N-97', None, None, None, None, '7/8"', 'N-97'),
        ('7/8" Normal', 'D-78', None, None, None, None, '7/8"', 'D-78'),
        ('3/4" Normal', 'D-78', None, None, None, None, '3/4"', 'D-78'),
        ('3/4" Normal', 'K', None, None, None, None, '3/4"', 'K'),
    ]
    conn.executemany(
        'INSERT INTO "tblrodgrades" ("size","grade","TACsize","TACdesc","stabsize","stabdesc","partedsize","partedgrade") VALUES (?,?,?,?,?,?,?,?)',
        rod_grades,
    )

    conn.execute('INSERT INTO "tblwells" ("WellName","notes") VALUES (?,?)', ("ISIS-05", "WLEG completion with TAC - Vertical Producer"))
    conn.execute('INSERT INTO "tblwelldata" ("WellName","WellRunDate","Both") VALUES (?,?,?)', ("ISIS-05", "2022-04-20", "ISIS-05 2022-04-20"))
    conn.execute('INSERT INTO "tblwelldata" ("WellName","WellRunDate","Both") VALUES (?,?,?)', ("ISIS-05", "2024-05-16", "ISIS-05 2024-05-16"))

    conn.execute(
        """INSERT INTO "tblpumpdata" (
            "WellName","WellRunDate","Both","Pumpdesignation","Vendor","Pumpsize","PumpType","Extension",
            "SV","DSV","TV","DTV","Openplunger","PlugSeat","PlungerClearance","PumpSN","PumpCOND",
            "StartDate","FailureDate","PumpLifetime","PumpLength","notes",
            "welltype","pumpreportdate","pumpinspectioncond",
            "barrelcond","barreldefect","barrelapparentfailure",
            "rodguidecond","rodguidedefect","rodguideapparentfailure",
            "barrelconnectorcond","barrelconnectordefect","barrelconnectorapparentfailure",
            "topextcond","botextcond",
            "SVcond","SVballcond","SVseatcond","SVballseattest","SVballseatcomment",
            "DSVcond","DSVballcond","DSVseatcond","DSVballseattest","DSVballseatcomment",
            "coup/pseatcond",
            "plungercond","plungerdefect","plungerapparentfailure",
            "vrodcond","vroddefect","vrodapparentfailure",
            "vrodbushingcond","vrodnutcond",
            "oplungercond","oplungerapparentfailure",
            "TVcond","TVapparentfailure","TVballcond","TVseatcond","TVballseattest","TVballseatcomment",
            "DTVcond","DTVballcond","DTVseatcond","DTVballseattest","DTVballseatcomment",
            "TVpseatcond",
            "hdownbushcond","mandrelcond","sleevecond","hdownringcond","hdownringapparentfailure",
            "inspectby","preparedby","comments"
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "ISIS-05", "2024-05-16", "ISIS-05 2024-05-16", "30-225-RHBM-20-6-2-2", "HF", "2.25", "Bottom", "2,2",
            "SS (A)", "SS (A)", "Monel (A)", "SS (A)", "Monel (A)", "Normal (A)", "-0.005", "96007", "B",
            "2024-05-16", "2024-08-04", 80, 26, 'Unswept length = 1.5"',
            "Vertical", "2024-08-18", "B",
            "Fair", "", "Good condition",
            "Scrap", "Wear", "Severe Wear from V.Rod and Top of it was broken",
            "Scrap", "Wear", "Severe Wear from V.Rod",
            "Fair", "Fair",
            "Fair", "Dull", "Cracked", "Not performed", "Crack in SV closed cage SEAT",
            "Fair", "OK", "OK", "Not performed", "Slight Dull, Test not performed",
            "Fair",
            "Scrap", "Thread galling", "Thread galling during tear down (TV side)",
            "Scrap", "Parted", "Parted valve rod just above opg collet (Flexing fatigue) (S.S)",
            "Fair", "Fair",
            "Scrap", "due to broken V.Rod inside it cannot be extracted",
            "Scrap", "Thread Galling during Tear down", "Dull", "OK", "Not performed", "Slight Dull, Test not performed",
            "Fair", "Dull", "OK", "Not performed", "Slight Dull, Test not performed",
            "Fair",
            "Fair", "Fair", "Fair", "Fair", "good condition",
            "M.Abdelhay", "Osama", "Pump still under repair (No spare parts available)\nValve rod was S.S with part number (HF 2-65M209)"
        ),
    )

    conn.execute(
        """INSERT INTO "tblTBGdata" (
            "WellName","WellRunDate","Both","RIHtbgrig","tbgrihrigblock","CompletionType",
            "WLEGDepth","WLEGNo","WLEGLength","WLEGSize","WLEGCond",
            "SNDepth","SNNo","SNLength","SNSize","SNCond",
            "TBGBetweenSN-TACNo","TBGBetweenSN-TACLength","TBGBetweenSN-TACSize","TBGBetweenSN-TACCond",
            "TACDepth","TACNo","TACLength","TACSize","TACdescr","TACCond",
            "TBG1AboveTACNo","TBG1AboveTACLength","TBG1AboveTACSize","TBG1AboveTACCond",
            "ORKB","TopPerfDepth","BotPerfDepth","PBTD","KTHFlangeSize","KTHFlangeType",
            "Distanceundercollar(X)","TBGStretch","TACOverPull","WeightIndicator(Start)","WeightIndicator(End)",
            "TBGStartDate","Generalcomments"
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "ISIS-05", "2024-05-16", "ISIS-05 2024-05-16", "EDC-31", "1500", "WLEG",
            "6234", 1, "0.62", "3 1/2 EUE", "A",
            "6203", 1, "0.62", "3 1/2 MECH (HF)", "A",
            3, "92.19", "3 1/2 EUE (FLAT)", "A",
            "6110", 1, "3.14", "7", "w/mandrel 3", "A",
            198, "6088", "3 1/2 EUE (FLAT)", "B (White Band)",
            "22", "6754", "6770", "6909", "11 X 5K X 3.5", "(2) with upper neck",
            "7", "22.75", "23000", "52000", "75000",
            "2024-05-16", ""
        ),
    )

    tbg_items = [
        ("ISIS-05", "2024-05-16", "TBG Jnts", "198", "6088", "3 1/2", "3 1/2 EUE (FLAT)", "B (White Band)", "6110", "WLEG", "1", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "TAC", "1", "3.14", "7", "w/mandrel 3", "A", "6113.14", "WLEG", "2", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "TBG Jnts", "3", "92.19", "3 1/2", "3 1/2 EUE (FLAT)", "A", "6202.38", "WLEG", "3", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "SN", "1", "0.62", "3 1/2", "3 1/2 MECH (HF)", "A", "6203", "WLEG", "4", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "TBG Jnts", "1", "30.9", "3 1/2", "3 1/2 EUE (FLAT)", "B (White Band)", "6233.38", "WLEG", "5", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "WLEG", "1", "0.62", "3 1/2", "3 1/2 EUE", "A", "6234", "WLEG", "6", "ISIS-05 2024-05-16"),
    ]
    conn.executemany(
        'INSERT INTO "tblTBGdatanew" ("WellName","WellRunDate","itemtype","itemno","itemlength","itemsize","itemdescrip","itemcond","itemdepth","completiontype","intervalno","Both") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        tbg_items,
    )

    rods = [
        ("ISIS-05", "2024-05-16", "Pony Rod 2'", "1", 2.0, "N-97", "A", "1", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "Polished Rod", "1", 30.0, "30 ft X 1.5 in, HF", "A", "2", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "Pony Rod 4'", "2", 8.0, "N-97", "A + B", "3", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "Pony Rod 6'", "1", 6.0, "N-97", "A", "4", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", '1" Normal', "47", 1175.0, "N-97", "A", "5", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", '1" Normal', "46", 1150.0, "N-97", "B", "6", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", '7/8" Normal', "40", 1000.0, "N-97", "A", "7", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", '7/8" Normal', "84", 2100.0, "N-97", "B", "8", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", '1" Guided', "28", 700.0, "N-97", "A", "9", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "Stab Bar 3.5", "1", 4.0, "4ft X 1in (7/8in pin)", "A", "10", "ISIS-05 2024-05-16"),
        ("ISIS-05", "2024-05-16", "Sand Screen", "1", 8.0, "1.5in X 8ft", "A", "11", "ISIS-05 2024-05-16"),
    ]
    conn.executemany(
        'INSERT INTO "tblrodsdatanew" ("WellName","WellRunDate","itemtype","itemno","itemlength","itemsize","itemcond","intervalno","Both") VALUES (?,?,?,?,?,?,?,?,?)',
        rods,
    )
    conn.execute(
        'INSERT INTO "tblrodstringdata2" ("WellName","WellRunDate","Both","RigName","rodrihrigblock","JobType","RodRotator","Changed1","Changed2","Supervisor","Foreman") VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        ("ISIS-05", "2024-05-16", "ISIS-05 2024-05-16", "EDC-31", "1500", "P/R", "NO", "TBG, DHP", "Rod string", "M.Fathy", "Mahmoud Abd El-Hay (DHP & TAC set) / Mansour (rods)"),
    )

    conn.execute(
        """INSERT INTO "tblwoverdata" (
            "WellName","WellRunDate","Both","SDDate","WOverDate","WellLifeTime",
            "RIGNAMErods","T/BlockWTrod","Calrodstringwt","Matched","CalOverPull","ActOverPull",
            "PumpUnset","BackOff","Wearcomments","Scalepresence","WellStatus","W/Oreason",
            "1stP/Tresult","Pressuredroppedfrom","Pressuredroppedto","within/minutes",
            "Normreadingsfrom","Normreadingsto","Normstatus","Pulledsandscreen","PulledDHPtest",
            "MSVtest","MSVtestresult","TACunsettrial","PSNringtest","Wellspeed","SL",
            "Rodrotatorstatus","polishedrodstatus","Wellfailurereason","pumpfailure type",
            "Generalnotes","Supervisor","Foreman"
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "ISIS-05", "2024-05-16", "ISIS-05 2024-05-16", "2024-08-04", "2024-09-13", 80,
            "EDC-31", "5000", 15679, "Yes", 68955, 7000,
            "Yes", "No", "Pulled rods had wear", "No", "S/D due to DHproblem", "Pump replacement",
            "There was a leakage", "500", "300", "3",
            "0.1", "0.2", "Normal Readings", "Fully plugged with Sands", "Not Working",
            "Yes", "HOLD", "No", "Not performed", "6.8", "144",
            "N/A", "Still working", "Pump Problem", "Waiting on pump inspection",
            '1- Changed DH pump only, and the 28, D-78, 1" rod taper, as the MSV was hold.\n2- The pulled pump had a very low discharge when tested at the rig after POOH.',
            "", "Tamer Abdelnaby, Ahmed Elwakel, Mahmoud Soliman"
        ),
    )

    wear_rows = [
        ("ISIS-05", "2024-05-16", '1" Normal', "N-97", "93", "1", "93", "59", "2384", "Slight", "Slight", "-", "-", "-", "-", "ISIS-05 2024-05-16", "1"),
        ("ISIS-05", "2024-05-16", '7/8" Normal', "N-97", "124", "1", "124", "2384", "5484", "Slight", "-", "-", "-", "-", "-", "ISIS-05 2024-05-16", "2"),
        ("ISIS-05", "2024-05-16", '1" Guided', "D-78", "28", "1", "28", "5484", "6184", "-", "-", "-", "-", "Moderate", "-", "ISIS-05 2024-05-16", "3"),
    ]
    conn.executemany(
        'INSERT INTO "tblwearcommentsdatanew" ("WellName","WellRunDate","Rodtype","Rodgrade","Noofrods","FRod","TRod","FDepth","TDepth","Coupling","Shoulder","Neck","Body","Guides","Wearrate","Both","intervalno") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        wear_rows,
    )
    conn.execute(
        'INSERT INTO "tblwearcommentsdata2" ("WellName","WellRunDate","Both","Pitting","General Comments") VALUES (?,?,?,?,?)',
        ("ISIS-05", "2024-05-16", "ISIS-05 2024-05-16", "Yes", '1- There was pitting into some of the rods couplings.\n2- There was moderate wear in the rod guides of the 1", guided rods.'),
    )
    conn.execute('INSERT OR IGNORE INTO "app_users" ("username","full_name","role") VALUES (?,?,?)', ("admin", "Production Engineering Admin", "admin"))
