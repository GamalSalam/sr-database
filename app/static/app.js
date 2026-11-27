function goToWell(basePath, inputId) {
  const val = (document.getElementById(inputId)?.value || "").trim();
  if (!val) { alert("Please type or select a Well Name first."); return; }
  window.location.href = basePath + "?well=" + encodeURIComponent(val);
}

async function triggerBackup() {
  const btn = document.getElementById("btn-backup");
  if (btn) { btn.disabled = true; btn.textContent = "Backing up..."; }
  try {
    const res = await fetch("/api/backup", { method: "POST" });
    const data = await res.json();
    alert("Hot SQLite backup created:\n" + data.backup_file);
    window.location.reload();
  } catch (err) { alert("Backup failed: " + err); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "Backup DB Now"; } }
}

async function loadWellDates(wellSelectId, dateSelectId) {
  const well = document.getElementById(wellSelectId)?.value;
  const dateSel = document.getElementById(dateSelectId);
  if (!well || !dateSel) return;
  try {
    const url = "/api/well-dates/" + encodeURIComponent(well);
    const data = window.SROfflineCache
      ? await window.SROfflineCache.cachedFetchJson(url)
      : await (await fetch(url)).json();
    dateSel.innerHTML = '<option value="">-- Latest Run Date --</option>';
    (data.dates || []).forEach(d => {
      const opt = document.createElement("option");
      opt.value = d; opt.textContent = d;
      dateSel.appendChild(opt);
    });
  } catch (e) { console.error(e); }
}

// Workover Page: When user types/selects Well Name from datalist -> fetch historical Run Dates & reload latest
let _woWellTimer = null;
function onWorkoverWellInput(val) {
  clearTimeout(_woWellTimer);
  const well = (val || "").trim();
  const listEl = document.getElementById("wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/workover?well=" + encodeURIComponent(well);
  }
}

function onWorkoverRunDateSelect(runDateVal) {
  const well = document.getElementById("wo_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  if (runDateVal === "__NEW__") {
    const customBox = document.getElementById("wo_new_date_wrap");
    if (customBox) customBox.classList.remove("hidden");
    return;
  }
  window.location.href = "/workover?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}

function updateCompletionUI() {
  const sel = document.getElementById("comp_type_select");
  if (!sel) return;
  const val = sel.value;
  const hasTac = !val.includes("No TAC") && val !== "HYD PKR";
  const isWleg = val.startsWith("WLEG");
  const isBplug = val.startsWith("B.PLUG");
  const isPkr = val === "HYD PKR";

  document.querySelectorAll(".comp-tac").forEach(el => el.classList.toggle("hidden", !hasTac));
  document.querySelectorAll(".comp-wleg").forEach(el => el.classList.toggle("hidden", !(isWleg || isPkr)));
  document.querySelectorAll(".comp-bplug").forEach(el => el.classList.toggle("hidden", !isBplug));
  document.querySelectorAll(".comp-pkr").forEach(el => el.classList.toggle("hidden", !isPkr));
}

function calcPumpDesignation() {
  const sz = document.getElementById("p_size")?.value || "";
  const pt = document.getElementById("p_type")?.value || "";
  const bl = document.getElementById("p_blen")?.value || "";
  const ext = document.getElementById("p_ext")?.value || "";
  const lenEl = document.getElementById("p_len");
  const desEl = document.getElementById("p_desig");
  const extParts = ext.split(",").map(x => parseFloat(x.trim()) || 0);
  const totalLen = (parseFloat(bl) || 0) + extParts.reduce((a, b) => a + b, 0) + 2;
  if (lenEl && bl) lenEl.value = totalLen;
  if (desEl && sz && bl && !desEl.dataset.manual) {
    const code = pt.toLowerCase().includes("bottom") ? "RHBM" : "RHAM";
    const szCode = sz.replace(".", "");
    desEl.value = `30-${szCode}-${code}-${bl}-6-${ext.replace(",", "-")}`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  updateCompletionUI();
  const desEl = document.getElementById("p_desig");
  if (desEl && !desEl.value.trim()) {
    calcPumpDesignation();
  }
});


// =============================================================================
// STEP 2: PULLED ROD WEAR — WELL/DATE AUTO-LOAD + MULTI-SECTION INTERVALS
// =============================================================================
function onWearWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("wear_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/wear?well=" + encodeURIComponent(well);
  }
}

function onWearRunDateSelect(runDateVal) {
  const well = document.getElementById("wear_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  if (runDateVal === "__NEW__") {
    const customBox = document.getElementById("wear_new_date_wrap");
    if (customBox) customBox.classList.remove("hidden");
    return;
  }
  window.location.href = "/wear?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}

function _buildWearRowHTML(intervalNo, defaultType, defaultGrade, nextFRod, prevTDepth) {
  const rt = document.getElementById("tpl_wear_rod_type").innerHTML;
  const rg = document.getElementById("tpl_wear_rod_grade").innerHTML;
  const c1 = document.getElementById("tpl_wear_severity_Coupling").innerHTML;
  const c2 = document.getElementById("tpl_wear_severity_Shoulder").innerHTML;
  const c3 = document.getElementById("tpl_wear_severity_Neck").innerHTML;
  const c4 = document.getElementById("tpl_wear_severity_Body").innerHTML;
  const c5 = document.getElementById("tpl_wear_severity_Guides").innerHTML;
  const c6 = document.getElementById("tpl_wear_severity_Wearrate").innerHTML;

  const tr = document.createElement("tr");
  tr.className = "wear-section-row";
  tr.innerHTML = `
    <td><input name="intervalno" value="${intervalNo}" readonly style="width:40px; text-align:center; background:#e2e8f0; font-weight:700;" /></td>
    <td>${rt}</td>
    <td>${rg}</td>
    <td><input name="Noofrods" value="" class="w-no" style="width:52px;" oninput="recalcWearDepths()" /></td>
    <td><input name="FRod" value="${nextFRod || ''}" class="w-frod" style="width:54px;" oninput="recalcWearRow(this)" /></td>
    <td><input name="TRod" value="" class="w-trod" style="width:54px;" oninput="recalcWearRow(this)" /></td>
    <td><input name="FDepth" value="${prevTDepth || ''}" class="w-fdepth" style="width:66px;" /></td>
    <td><input name="TDepth" value="" class="w-tdepth" style="width:66px;" /></td>
    <td>${c1}</td>
    <td>${c2}</td>
    <td>${c3}</td>
    <td>${c4}</td>
    <td>${c5}</td>
    <td>${c6}</td>
    <td style="text-align:center;"><button type="button" class="btn btn-danger" onclick="removeWearRow(this)" title="Remove section">&times;</button></td>
  `;
  if (defaultType) {
    const selT = tr.querySelector('select[name="Rodtype"]');
    if (selT) selT.value = defaultType;
  }
  if (defaultGrade) {
    const selG = tr.querySelector('select[name="Rodgrade"]');
    if (selG) selG.value = defaultGrade;
  }
  return tr;
}

function addWearSection(btn) {
  const block = btn.closest(".wear-interval-block");
  if (!block) return;
  const intervalNo = block.dataset.intervalNo || "1";
  const tbody = block.querySelector(".wear-sections-body");
  const lastRow = tbody.querySelector("tr:last-child");

  let defaultType = "";
  let defaultGrade = "";
  let nextFRod = "1";
  let prevTDepth = "";

  if (lastRow) {
    defaultType = lastRow.querySelector('select[name="Rodtype"]')?.value || "";
    defaultGrade = lastRow.querySelector('select[name="Rodgrade"]')?.value || "";
    const lastTRod = parseInt(lastRow.querySelector(".w-trod")?.value || "0", 10);
    if (!isNaN(lastTRod) && lastTRod > 0) nextFRod = String(lastTRod + 1);
    prevTDepth = lastRow.querySelector(".w-tdepth")?.value || "";
  }

  tbody.appendChild(_buildWearRowHTML(intervalNo, defaultType, defaultGrade, nextFRod, prevTDepth));
}

function addWearInterval() {
  const container = document.getElementById("wear_intervals_container");
  if (!container) return;
  const existingBlocks = container.querySelectorAll(".wear-interval-block");
  const nextIntNo = String(existingBlocks.length + 1);

  let prevTDepth = container.dataset.baseDepth || "59";
  if (existingBlocks.length > 0) {
    const lastBlock = existingBlocks[existingBlocks.length - 1];
    const lastRow = lastBlock.querySelector("tr.wear-section-row:last-child");
    if (lastRow && lastRow.querySelector(".w-tdepth")?.value) {
      prevTDepth = lastRow.querySelector(".w-tdepth").value;
    }
  }

  const div = document.createElement("div");
  div.className = "sub-card wear-interval-block";
  div.dataset.intervalNo = nextIntNo;
  div.innerHTML = `
    <div class="sub-card-title">
      <span>Interval #${nextIntNo} — New Rod Taper</span>
      <div style="display:flex; gap:6px;">
        <button type="button" class="btn btn-success" style="padding:4px 10px; font-size:11.5px;" onclick="addWearSection(this)">+ Add Section to Interval #${nextIntNo}</button>
      </div>
    </div>
    <div class="table-wrap">
      <table class="wear-grid-table">
        <thead>
          <tr>
            <th style="width:45px;">Int#</th><th style="min-width:115px;">Rod Type</th><th style="min-width:75px;">Grade</th>
            <th style="width:58px;">No</th><th style="width:60px;">F/Rod</th><th style="width:60px;">T/Rod</th>
            <th style="width:72px;">F/Depth</th><th style="width:72px;">T/Depth</th>
            <th style="min-width:88px;">Coupling</th><th style="min-width:88px;">Shoulder</th><th style="min-width:88px;">Neck</th>
            <th style="min-width:88px;">Body</th><th style="min-width:88px;">Guides</th><th style="min-width:88px;">Wear Rate</th>
            <th style="width:36px;">Del</th>
          </tr>
        </thead>
        <tbody class="wear-sections-body"></tbody>
      </table>
    </div>
  `;
  div.querySelector(".wear-sections-body").appendChild(_buildWearRowHTML(nextIntNo, '1" Normal', "N-97", "1", prevTDepth));
  container.appendChild(div);
}

function removeWearRow(btn) {
  const tr = btn.closest("tr");
  const tbody = tr?.parentElement;
  const block = btn.closest(".wear-interval-block");
  if (tr) tr.remove();
  if (tbody && tbody.querySelectorAll("tr").length === 0 && block) {
    block.remove();
  }
  recalcWearDepths();
}

function recalcWearRow(el) {
  const tr = el.closest("tr");
  if (!tr) return;
  const fr = parseInt(tr.querySelector(".w-frod")?.value || "", 10);
  const trd = parseInt(tr.querySelector(".w-trod")?.value || "", 10);
  if (!isNaN(fr) && !isNaN(trd) && trd >= fr) {
    tr.querySelector(".w-no").value = String(trd - fr + 1);
  }
  recalcWearDepths();
}

function recalcWearDepths() {
  const container = document.getElementById("wear_intervals_container");
  if (!container) return;
  let curDepth = parseFloat(container.dataset.baseDepth || "59") || 59;
  container.querySelectorAll("tr.wear-section-row").forEach(tr => {
    const noVal = parseFloat(tr.querySelector(".w-no")?.value || "0");
    const fdEl = tr.querySelector(".w-fdepth");
    const tdEl = tr.querySelector(".w-tdepth");
    if (!isNaN(noVal) && noVal > 0) {
      if (fdEl) fdEl.value = String(Math.round(curDepth));
      curDepth += noVal * 25;
      if (tdEl) tdEl.value = String(Math.round(curDepth));
    }
  });
}


// =============================================================================
// STEP 4 & 6: DOWNHOLE PUMP & WORKSHOP TEAR DOWN AUTO-LOADERS
// =============================================================================
function onPumpWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("pump_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/pumps?well=" + encodeURIComponent(well);
  }
}

function onPumpRunDateSelect(runDateVal) {
  const well = document.getElementById("pump_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  if (runDateVal === "__NEW__") {
    const customBox = document.getElementById("pump_new_date_wrap");
    if (customBox) customBox.classList.remove("hidden");
    return;
  }
  window.location.href = "/pumps?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}

function recalcPumpLifetime() {
  const s = document.getElementById("p_start")?.value;
  const f = document.getElementById("p_fail")?.value;
  const lifeEl = document.getElementById("p_life");
  if (s && f && lifeEl) {
    const days = Math.max(0, Math.round((new Date(f) - new Date(s)) / 86400000));
    lifeEl.value = days;
  }
}

function onTdWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("td_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/pumps/teardown?well=" + encodeURIComponent(well);
  }
}

function onTdRunDateSelect(runDateVal) {
  const well = document.getElementById("td_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  window.location.href = "/pumps/teardown?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}


// =============================================================================
// STEP 3: TUBING STRING (TBG) — WELL/DATE AUTO-LOAD + DYNAMIC ITEMS
// =============================================================================
function onTbgWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("tbg_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/tbg?well=" + encodeURIComponent(well);
  }
}

function onTbgRunDateSelect(runDateVal) {
  const well = document.getElementById("tbg_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  window.location.href = "/tbg?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}

function addTbgRow() {
  const tbody = document.getElementById("tbg_items_body");
  if (!tbody) return;
  const rows = tbody.querySelectorAll("tr.tbg-item-row");
  if (rows.length === 0) tbody.innerHTML = "";
  const nextOrd = rows.length + 1;
  const tplType = document.getElementById("tpl_tbg_item_type").innerHTML;
  const tplCond = document.getElementById("tpl_tbg_cond").innerHTML;
  const tr = document.createElement("tr");
  tr.className = "tbg-item-row";
  tr.innerHTML = `
    <td><input name="intervalno" value="${nextOrd}" style="text-align:center; font-weight:700;" /></td>
    <td>${tplType}</td>
    <td><input name="itemno" value="1" /></td>
    <td><input name="itemlength" value="" placeholder="30.9" /></td>
    <td><input name="itemsize" value="3 1/2" placeholder="3 1/2" /></td>
    <td><input name="itemdescrip" value="" placeholder="3 1/2 EUE (FLAT)" /></td>
    <td>${tplCond}</td>
    <td style="text-align:center;"><button type="button" class="btn btn-danger" onclick="removeTbgRow(this)" title="Remove item">&times;</button></td>
  `;
  tbody.appendChild(tr);
}

function removeTbgRow(btn) {
  const tr = btn.closest("tr");
  if (tr) tr.remove();
  document.querySelectorAll("#tbg_items_body tr.tbg-item-row").forEach((row, idx) => {
    const ord = row.querySelector('input[name="intervalno"]');
    if (ord) ord.value = String(idx + 1);
  });
}

// =============================================================================
// STEP 5: ROD STRING (RIH) — WELL/DATE AUTO-LOAD + DYNAMIC ITEMS
// =============================================================================
function onRodsWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("rods_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/rods?well=" + encodeURIComponent(well);
  }
}

function onRodsRunDateSelect(runDateVal) {
  const well = document.getElementById("rods_well_input")?.value?.trim();
  if (!well || !runDateVal) return;
  window.location.href = "/rods?well=" + encodeURIComponent(well) + "&run_date=" + encodeURIComponent(runDateVal);
}

function addRodRow() {
  const tbody = document.getElementById("rods_items_body");
  if (!tbody) return;
  const rows = tbody.querySelectorAll("tr.rod-item-row");
  if (rows.length === 0) tbody.innerHTML = "";
  const nextOrd = rows.length + 1;
  const tplType = document.getElementById("tpl_rod_item_type").innerHTML;
  const tplCond = document.getElementById("tpl_rod_cond").innerHTML;
  const tr = document.createElement("tr");
  tr.className = "rod-item-row";
  tr.innerHTML = `
    <td><input name="intervalno" value="${nextOrd}" style="text-align:center; font-weight:700;" /></td>
    <td>${tplType}</td>
    <td><input name="itemno" value="1" oninput="autoRodLength(this)" /></td>
    <td><input type="number" step="0.1" name="itemlength" value="25" /></td>
    <td><input name="itemsize" value="N-97" placeholder="N-97" /></td>
    <td>${tplCond}</td>
    <td style="text-align:center;"><button type="button" class="btn btn-danger" onclick="removeRodRow(this)" title="Remove item">&times;</button></td>
  `;
  tbody.appendChild(tr);
}

function removeRodRow(btn) {
  const tr = btn.closest("tr");
  if (tr) tr.remove();
  document.querySelectorAll("#rods_items_body tr.rod-item-row").forEach((row, idx) => {
    const ord = row.querySelector('input[name="intervalno"]');
    if (ord) ord.value = String(idx + 1);
  });
}

function autoRodLength(noInput) {
  const tr = noInput.closest("tr");
  if (!tr) return;
  const typeVal = (tr.querySelector('select[name="itemtype"]')?.value || "").toLowerCase();
  const cnt = parseFloat(noInput.value || "");
  if (isNaN(cnt)) return;
  // Auto-calculate 25ft per standard/guided rod; leave pony/polished/stab/sand screen manual
  if (typeVal.includes("normal") || typeVal.includes("guided")) {
    const lenEl = tr.querySelector('input[name="itemlength"]');
    if (lenEl) lenEl.value = String(cnt * 25);
  }
}

// =============================================================================
// REPORTS HUB: TYPE-TO-FILTER WELL AUTO-LOADER
// =============================================================================
function onReportWellInput(val) {
  const well = (val || "").trim();
  const listEl = document.getElementById("rpt_wells_datalist");
  if (!listEl || !well) return;
  const matched = Array.from(listEl.options).some(opt => opt.value.toLowerCase() === well.toLowerCase());
  if (matched) {
    window.location.href = "/reports?well=" + encodeURIComponent(well);
  }
}
