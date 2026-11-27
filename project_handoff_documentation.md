# SR Database Server — Project Handoff Document

**Owner / Contact:** Gamal Salama (`Gamal.Salama@khalda-eg.com` | `+201289630938`)  
**Stack:** FastAPI + SQLite (WAL Mode) + Jinja2 + OpenPyXL  
**Packaging:** PyInstaller (`--onedir` → `SRDatabaseServer.exe` + `_internal/`)  
**Purpose:** Standalone multi-user LAN web server replacing a legacy 16-table Microsoft Access Sucker Rod & Downhole Equipment database with zero client installation.

---

## 1. Architecture & File Map

```text
SR_Database_WebServer/
├── data/
│   ├── sr_database.db              # SQLite WAL database (auto-created on first run)
│   └── builtin_layout.json         # External editable page layout JSON (auto-copied on first run, updated by Admin UI)
├── backups/
│   └── sr_database_YYYYMMDD_HHMMSS.db  # Hot backups (auto-scheduled every 24h, keeps last 30)
└── app/
    ├── main.py                     # FastAPI routes, AuthMiddleware, 6 workflow steps, reports, backup, export & restore APIs
    ├── auth.py                     # PBKDF2 password hashing, SQLite sessions, /setup, /login, /account, /admin/users
    ├── database.py                 # SQLite WAL engine, date normalization, pump views, Data-Only/Full Excel/CSV exports, 24h backup scheduler
    ├── backup_restore.py           # In-app backup restore/reload engine (.db hot restore, .zip CSV bundle, .xlsx Full DB)
    ├── schema.sql                  # Exact 16 legacy Access tables + auth/session/extension tables
    ├── seed_data.py                # 48 configurable dropdown lists + ISIS-05 sample data
    ├── calculations.py             # Wellbore Sketch builder (new + legacy tblrodstringdata1/2 fallback + Pump Intake)
    ├── custom_fields.py            # Dynamic Page Layout & Custom Fields (EAV) engine + JSON sync + Admin UI + JSON API
    ├── builtin_layout.json         # Bundled default JSON inside .exe (read-only template copied to data/builtin_layout.json)
    ├── insights.py                 # Well History API (Failures, Wear Profile, Pumps, Rod Strings) + Dashboard Status Rules
    ├── excel_export.py             # Styled OpenPyXL report workbooks (Sketch, Workover, Pump Tear Down)
    ├── accdb_importer.py           # Access (.accdb/.mdb) importer (64/32-bit PowerShell COM + pyodbc)
    ├── static/
    │   ├── style.css               # Core styling (incl. Admin nav & responsive layout)
    │   ├── insights.css            # History panel & status chip styles
    │   ├── app.js                  # Auto-filter well loaders, wear/tbg/rod dynamic tables, pump calc
    │   ├── custom_fields.js        # Dynamic layout arranger, placeholders/defaults, inline custom fields & auto-saver
    │   └── well_history.js         # Well History UI + Multi-Run Side-View Depth vs. Wear Erosion SVG Plot
    └── templates/
        ├── base.html               # Collapsible sidebar, Dark Mode toggle, Admin Settings & Backup DB icon dropdowns, Save Guard
        ├── setup.html              # First-time Administrator password setup screen (/setup)
        ├── login.html              # Sign-in page (/login)
        ├── account.html            # User profile & self-service password change (/account)
        ├── forbidden.html          # 403 Admin Access Required page for Standard Users
        ├── dashboard.html          # Main Menu KPIs, Finished/In-Progress status badges, step chips & report links
        ├── wells.html              # Well Management + Well History mount point
        ├── workover.html           # Step 1: Workover & Pulled Data (tblwoverdata)
        ├── wear.html               # Step 2: Pulled Rod Wear multi-section intervals
        ├── tbg.html                # Step 3: New Tubing String (4 grouped cards + tally table, dynamic Completion Type)
        ├── pumps.html              # Step 4: New Downhole Pump specs (PlugSeat in Valves & Cages) + Export Pump Table (.xlsx)
        ├── rods.html               # Step 5: New Rod String + Section 3 Pump Intake (Below Pump)
        ├── pump_teardown.html      # Step 6: Workshop Tear Down inspection (25-item checklist)
        ├── reports.html            # Reports & Exports hub
        ├── report_view.html        # Printable HTML/PDF Wellbore Sketch, Work Over & Pump Tear Down reports
        ├── admin.html              # Dropdown list manager, Access importer, DB summary & Reload/Restore Backup panel
        ├── admin_users.html        # User Access Control (create/edit/disable/reset-password/delete users)
        ├── admin_custom_fields.html# Dynamic Page Layout & Custom Fields manager (Built-in + Custom, hints & defaults)
        └── admin_insights.html     # History panel columns & Dashboard "Finished" step rules
```

---

## 2. Database Schema & Normalization

### Legacy 16 Access Tables (Untouched for `.accdb` Compatibility)
`tblwells`, `tblwelldata`, `tblrodgrades`, `tblpumpdata`, `tblTBGdata`, `tblTBGdatanew`, `tblrodstringdata1`, `tblrodstringdata2`, `tblrodsdatanew`, `tblrodstracking1`, `tblrodstracking2`, `tblwoverdata`, `tblwearcommentsdata1`, `tblwearcommentsdata2`, `tblwearcommentsdatanew`, `tbldatanew`.

* **Automatic Date Normalization:** `normalize_all_dates()` standardizes all legacy Access date formats (`m/d/yyyy`, `dd-Mon-yy`, timestamps) into strict `YYYY-MM-DD`.

### Extension Tables & Normalized Views
| Object | Type | Purpose |
|---|---|---|
| `app_users` | Table | Authorized users (`username`, `full_name`, `role` [`admin`/`user`], `password_hash`, `is_active`, `last_login`) |
| `app_sessions` | Table | Server-side SHA-256 session tokens with expiration (supports instant revocation) |
| `app_dropdown_options` | Table | 48 Admin-configurable dropdown lists |
| `app_audit_log` | Table | Change history for inserts, updates, deletes, logins, and admin edits (attributed to signed-in `username`) |
| `app_custom_fields` | Table | Dynamic custom field definitions (`field_key`, `label`, `input_type`, `target_page`, `section_key`, `sort_order`, `placeholder`, `default_value`, `active`) |
| `app_custom_values` | Table | EAV values keyed by `UNIQUE(field_id, WellName, WellRunDate)` |
| `app_builtin_fields` | Table | Seeded from `data/builtin_layout.json` and synced back on Admin edits (labels, order, visibility, grey placeholders, default values) |
| `app_pump_intake_items` | Table | Below-pump intake items (Sand Screen, Dip Tube, Gas Separator, Mud Anchor) per run |
| `app_settings` | Table | Configurable history columns, max runs, and dashboard "Finished" rules |
| `vw_new_dh_pump_data` | View | Normalized 25-column New DH Pump view (equivalent to legacy Access `pumpdata` query) |
| `vw_pump_teardown_data` | View | Normalized Workshop Tear Down view (filtered to inspected pumps) |

---

## 3. Completed Features & Key Workflows

1. **Unified 6-Step Data Entry with Save Guard**
   * Every data-entry page uses type-to-filter Well Name + chronological Run Date auto-loaders.
   * **Unified Save Guard (Frontend + Backend):** Clicking Save without a Well Name or Run Date on any of the 6 pages is blocked cleanly with a top error banner and auto-focus on the missing selector; blank-well records can never be written to SQLite.
2. **Legacy & New Wellbore Sketch Rod String + Pump Intake**
   * `calculations.py` checks if a run in `tblrodsdatanew` has a full string (including `Polished Rod`). If an imported Access run only has taper rows, it automatically rebuilds the full top-to-bottom string (`Pony Rods → Polished Rod → Tapers → Stab Bar`) and below-pump items (`Dip Tube`, `Sand Screen`) from `tblrodstringdata1/2`.
   * New runs entered via `/rods` support **Section 3: Pump Intake (Below Pump)** (`app_pump_intake_items`), rendered directly under the `PUMP` row in both HTML/PDF (`report_view.html`) and Excel (`excel_export.py`).
3. **Dynamic Page Layout, Two `builtin_layout.json` Files & Custom Fields (`/admin/custom-fields`)**
   * **How the two `builtin_layout.json` files work:**
     * **Bundled default (`app/builtin_layout.json`):** Packed inside `SRDatabaseServer.exe` (`_internal/app/builtin_layout.json`) as a read-only factory template.
     * **External runtime file (`data/builtin_layout.json`):** Auto-copied next to the `.exe` (`dist/SRDatabaseServer/data/builtin_layout.json`) on first run. Whenever an Admin renames, reorders, hides/shows, or updates the **Grey Hint (Placeholder)** / **Default Value** of a built-in field in `/admin/custom-fields`, SQLite (`app_builtin_fields`) **and** `data/builtin_layout.json` are both updated automatically. If you edit `data/builtin_layout.json` manually, click **Reload from builtin_layout.json** in `/admin/custom-fields` to pull changes into SQLite.
   * Admin selects a target page to view all sections and fields (Built-in + Custom, including `Notes`, `Pump Designation`, and comment boxes) in exact order, add custom fields directly into any section at any `sort_order`, configure grey placeholder hints and default values, and save custom values automatically alongside the main form.
   * Custom fields added to **Step 4 (`pumps`)** (e.g., *Unswept Length*) automatically appear inside the **Downhole Pump Data** table on the Wellbore Sketch and as extra columns in the New Pump Excel export.
   * **Step 3 (`tbg.html`) & Step 4 (`pumps.html`) Card Organization:**
     * Step 3 splits `tblTBGdata` into 4 logical cards (`tbg_comp_depths`, `tbg_rig_weights`, `tbg_wellhead`, `tbg_dates_notes`) plus Section 5 tally table; `_ensure_layout_schema()` automatically migrates any legacy `tbg_header` rows in existing SQLite DBs.
     * Step 4 places `PlugSeat` inside the **Valves & Cages** (`pump_valves`) card (also auto-migrated for existing DBs).
4. **Well History Panel & Multi-Run Wear Erosion Plot (`/wells`)**
   * **Failure History:** Configurable columns per run (`/admin/insights`).
   * **Side-View Erosion Profile (Inline SVG):** Vertical axis = Depth (ft ORKB); Horizontal axis = Extent of Wear (`None` → `Severe`). Overlays all past runs on one chart with clickable run toggle chips, component filter (`Overall Max`, `Coupling`, `Shoulder`, `Neck`, `Body`, `Guides`, `Wear Rate`), and hover detail inspector.
   * **Downhole Pump History & Rod String History:** Chronological tables across previous runs with direct links to Pump Data, Tear Down Report, Rod String, and Wellbore Sketch.
5. **Dashboard "Finished Reports" View (`/`)**
   * Filterable by `All`, `Finished`, and `In Progress`, with per-step completion chips (`W/O`, `Wear`, `TBG`, `Pump`, `Rods`, `TD`) and one-click report badges.
6. **Backups, Clean Exports, In-App Restore & UI Polish**
   * **24-hour automatic hot backups** (`backups/sr_database_*.db`, rolling 30 files, plus collision-safe `_pre_restore` safety snapshots).
   * **Top-Bar `Admin Settings` & `Backup DB` Icon Menus (`base.html`, Admin-only):**
     * `Admin Settings` opens an icon dropdown linking to `/admin`, `/admin/users`, `/admin/custom-fields`, `/admin/insights`, and `/admin#restore-backup`.
     * `Backup DB` provides Hot `.db` backup, **Data-Only** `.xlsx` (`/api/backup/excel`), **Data-Only** CSV Bundle `.zip` (`/api/backup/csv-zip`), and a link to **Restore / Reload Backup**.
   * **Clean Data-Only vs. Full System Exports:** By default (`include_program=False`), `.xlsx` and `.zip` backups exclude all `app_*` program tables and export only engineering `vw_*` views and `tbl*` tables. Admins can tick **Include program/system tables** on `/admin` (`?include_program=1`) when needed (always excluding `app_sessions` and `app_users.password_hash`).
   * **In-App Backup Restore / Reload (`/admin#restore-backup`):**
     * **Option A (`POST /admin/restore-backup`):** Restore directly from any `.db` file saved in `/backups` (path-traversal protected).
     * **Option B (`POST /admin/restore-upload`):** Upload and reload a `.db`, `.zip` (CSV Bundle), or `.xlsx` (Full DB Excel) backup.
     * Validates file integrity/core tables and takes an automatic `_pre_restore` safety backup first. `.db` restores the full database (preserving the current Admin session if valid in the restored DB); `.zip`/`.xlsx` reload operational and config tables while keeping existing `app_users` and `app_sessions` intact.
   * **Dedicated New Pump Table Export:** One-click `/api/export/new-pump-excel` on `/pumps` (styled `.xlsx` with custom pump fields included).
   * **UI Enhancements:** Collapsible left sidebar (hamburger toggle), high-contrast Dark Mode toggle (persisted in `localStorage`, while keeping `.report-sheet` crisp white for print/PDF), and minimal offline SVG icons.
7. **Authentication & User Access Control (`/setup`, `/login`, `/admin/users`, `/account`)**
   * **First-Time Setup (`/setup`):** Prompts on first open to set the initial Administrator password, then permanently locks `/setup`.
   * **Role-Based Access (`Admin` vs `Standard User`):** Every page and `/api/*` endpoint requires a valid `sr_session` cookie. `Standard User` accounts can view wells/reports and enter/edit operational data (Steps 1–6), while `/admin*` and `/api/backup*` require `Admin`.
   * **User Access Control (`/admin/users`):** Admins can create users, edit names/roles, reset passwords, disable/enable accounts (immediately invalidating active sessions), and delete users. Users can change their own password at `/account`. All changes in `app_audit_log` are attributed to the logged-in `username`.

---

## 4. Running & Packaging (PyInstaller `--onedir`)

### Development Run
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### PyInstaller Packaging Checklist
When rebuilding `SRDatabaseServer.exe`, ensure `app/builtin_layout.json` is bundled alongside `app/schema.sql`, `app/templates`, and `app/static`:
```bash
pyinstaller --onedir --name SRDatabaseServer ^
  --add-data "app/schema.sql;app" ^
  --add-data "app/builtin_layout.json;app" ^
  --add-data "app/templates;app/templates" ^
  --add-data "app/static;app/static" ^
  run_server.py
```

---

## 5. Quick Verification Checklist After Deployment
1. Open `/` → verify unauthenticated access redirects to `/setup` (first run) or `/login`; sign in as Admin and test creating/disabling a `Standard User` on `/admin/users` (confirming Standard Users get `403` on `/admin*`).
2. Open the top-bar **Admin Settings** icon menu → verify links to `/admin`, `/admin/users`, `/admin/custom-fields`, `/admin/insights`, and `/admin#restore-backup`.
3. Open `/admin/custom-fields` → verify all 6 pages load cleanly from `data/builtin_layout.json`, Step 3 (`tbg`) shows its 4 grouped cards, Step 4 (`pumps`) lists `PlugSeat` under **Valves & Cages**, and editing a grey placeholder / label updates `data/builtin_layout.json`.
4. Open `/tbg` and `/pumps` → confirm Step 3's 4 grouped cards + dynamic Completion Type visibility and Step 4's `Plug Seat` inside `Valves & Cages`.
5. Open `/reports/view/rptwellboresketch?well=KHL-32&run_date=2023-03-19` → verify legacy string renders `Pony Rod → Polished Rod → Tapers → Stab Bar → PUMP → Sand Screen`.
6. Open `/wells` and select a well with multiple wear runs → verify the Side-View Erosion Profile SVG, Pump History, and Rod String History render.
7. Click **Backup DB** in the top bar (as Admin) → verify `.db`, clean Data-Only `.xlsx`/`.zip` (no `app_*` sheets/CSVs unless **Include program/system tables** is checked on `/admin`), and test restoring from `/admin#restore-backup` (`.db`, `.zip`, or `.xlsx`).
8. Test **Dark Mode** and **Sidebar Toggle** buttons in the top bar, and confirm Save is blocked with a red alert when no Well/Run Date is selected.

---

## 6. Future Roadmap (Phase 2 — Post-Access Schema Normalization)
* **Current Decision:** Keep the 16 legacy Access tables intact during initial company rollout so `.accdb` imports and historical reports remain 100% compatible.
* **Planned Phase 2 (Once Microsoft Access is fully retired):**
  * Normalize to a relational core (`wells` → `well_runs` with `run_id` foreign keys and `ON DELETE CASCADE`, eliminating repeated text `(WellName, WellRunDate)`/`Both` columns).
  * Consolidate wide numbered Access tables (`tblrodstringdata1/2`, `tblwearcommentsdata1/2`, flat tally columns in `tblTBGdata`, `tblrodstracking1/2`) into row-based child tables with strict `REAL`/`INTEGER` column types.
  * Split `tblpumpdata` into dedicated RIH pump installation (`pump_runs`) and workshop teardown (`pump_inspections`) tables, keeping `accdb_importer.py` as an ETL adapter if legacy `.accdb` archives still need ingestion.