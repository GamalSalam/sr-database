/* Well History Panel — Failures, Side-View Wear Erosion Plot, Wear Table, DH Pump History & Rod String History.
   Mounts into <div id="hist-panel"> and reloads whenever the selected Well Name changes. */
(function () {
  var host = document.getElementById('hist-panel');
  if (!host) return;
  var WELL_SEL = 'input[name="WellName"],select[name="WellName"]';
  var DATE_SEL = 'input[name="WellRunDate"],select[name="WellRunDate"]';
  var lastWell = null, timer = null;
  var SEV = ['None', 'Light', 'Slight', 'Moderate', 'Severe'];
  var PALETTE = ['#0284c7', '#dc2626', '#16a34a', '#9333ea', '#d97706', '#0d9488', '#e11d48', '#4f46e5', '#65a30d', '#c026d3'];
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null && text !== '') e.textContent = text;
    return e;
  }
  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVG_NS, tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null && text !== '') e.textContent = text;
    return e;
  }
  function ctx() {
    var q = new URLSearchParams(location.search);
    var w = (document.querySelector(WELL_SEL) || {}).value || q.get('well') || '';
    var d = (document.querySelector(DATE_SEL) || {}).value || q.get('run_date') || '';
    return { well: w.trim(), run_date: d.trim().slice(0, 10) };
  }
  function table(headers, rows, currentIdx) {
    var wrap = el('div', { 'class': 'table-wrap', style: 'max-height:260px;' });
    var t = el('table'), thead = el('thead'), hr = el('tr');
    headers.forEach(function (h) { hr.appendChild(el('th', {}, h)); });
    thead.appendChild(hr); t.appendChild(thead);
    var tb = el('tbody');
    rows.forEach(function (cells, i) {
      var tr = el('tr', i === currentIdx ? { 'class': 'hist-current', title: 'Currently selected run' } : {});
      cells.forEach(function (c) {
        var td = el('td');
        if (c && c.nodeType) td.appendChild(c); else td.textContent = (c === '' || c == null) ? '—' : c;
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t); return wrap;
  }
  function sevBadge(n) { return el('span', { 'class': 'sev sev-' + n }, SEV[n] || String(n)); }
  function linkBadge(label, href, cls) {
    return el('a', { 'class': 'badge ' + (cls || 'badge-blue'), href: href, style: 'text-decoration:none; margin-right:4px;' }, label);
  }

  // ---------------- Side-View Depth vs. Wear Profile Plot (Multi-Run SVG) ----------------
  function buildWearProfileChart(wearList) {
    var runsWithDepth = (wearList || []).filter(function (w) { return w.intervals && w.intervals.length > 0; });
    if (!runsWithDepth.length) return null;

    var box = el('div', {
      style: 'border:1px solid #cbd5e1; border-radius:8px; background:#ffffff; padding:12px; margin:8px 0 14px 0;'
    });

    // Header + Component Filter
    var topBar = el('div', {
      style: 'display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:8px;'
    });
    var titleWrap = el('div');
    titleWrap.appendChild(el('strong', { style: 'font-size:13px; color:#0f2544;' }, 'Side-View Erosion Profile — Depth (ORKB) vs. Wear Severity'));
    titleWrap.appendChild(el('div', { style: 'font-size:11px; color:#64748b;' }, 'Click any run chip to show/hide it · Hover any interval for exact wear details'));
    topBar.appendChild(titleWrap);

    var ctrlWrap = el('div', { style: 'display:flex; align-items:center; gap:6px; font-size:12px;' });
    ctrlWrap.appendChild(el('label', { style: 'font-weight:600; color:#334155;' }, 'Metric:'));
    var compSel = el('select', { style: 'padding:4px 8px; font-size:12px; border:1px solid #cbd5e1; border-radius:5px;' });
    [
      ['max', 'Overall Max Wear'],
      ['Coupling', 'Coupling'],
      ['Shoulder', 'Shoulder'],
      ['Neck', 'Neck'],
      ['Body', 'Body'],
      ['Guides', 'Guides'],
      ['Wearrate', 'Wear Rate']
    ].forEach(function (pair) { compSel.appendChild(el('option', { value: pair[0] }, pair[1])); });
    ctrlWrap.appendChild(compSel);
    topBar.appendChild(ctrlWrap);
    box.appendChild(topBar);

    // Legend chips (toggleable per run)
    var visible = {};
    runsWithDepth.forEach(function (w) { visible[w.run_date] = true; });
    var legend = el('div', { style: 'display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px;' });
    box.appendChild(legend);

    // Chart + Hover Inspector container
    var plotRow = el('div', { style: 'display:flex; flex-wrap:wrap; gap:14px; align-items:flex-start;' });
    var svgWrap = el('div', { style: 'flex:1; min-width:340px; overflow-x:auto;' });
    var infoBox = el('div', {
      style: 'width:230px; font-size:12px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:10px; color:#334155;'
    }, 'Hover over any colored wear segment on the plot to inspect the run date, depth interval, rod taper, and component ratings.');
    plotRow.appendChild(svgWrap);
    plotRow.appendChild(infoBox);
    box.appendChild(plotRow);

    // Compute max depth across all runs
    var rawMax = 0;
    runsWithDepth.forEach(function (w) {
      w.intervals.forEach(function (iv) { if (iv.t_depth > rawMax) rawMax = iv.t_depth; });
    });
    var maxDepth = Math.max(1000, Math.ceil(rawMax / 500) * 500);

    function rankFor(iv, metric) {
      if (metric === 'max') return iv.max_rank || 0;
      return (iv.ranks && iv.ranks[metric] != null) ? iv.ranks[metric] : 0;
    }

    function draw() {
      var metric = compSel.value;
      svgWrap.textContent = '';

      // Rebuild legend chips
      legend.textContent = '';
      runsWithDepth.forEach(function (w, idx) {
        var col = PALETTE[idx % PALETTE.length];
        var on = !!visible[w.run_date];
        var chip = el('button', {
          type: 'button',
          style: 'display:inline-flex; align-items:center; gap:5px; padding:3px 9px; font-size:11.5px; border-radius:14px; cursor:pointer; border:1.5px solid ' + col + '; background:' + (on ? col : '#ffffff') + '; color:' + (on ? '#ffffff' : '#475569') + '; opacity:' + (on ? '1' : '0.55') + ';'
        });
        chip.appendChild(el('span', { style: 'width:8px; height:8px; border-radius:50%; background:' + (on ? '#ffffff' : col) + '; display:inline-block;' }));
        chip.appendChild(document.createTextNode(w.run_date));
        chip.addEventListener('click', function () { visible[w.run_date] = !visible[w.run_date]; draw(); });
        legend.appendChild(chip);
      });

      var W = 540, H = 380;
      var padL = 62, padR = 24, padT = 34, padB = 34;
      var pw = W - padL - padR, ph = H - padT - padB;
      var xOf = function (sev) { return padL + (sev / 4) * pw; };
      var yOf = function (depth) { return padT + (depth / maxDepth) * ph; };

      var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, width: '100%', style: 'max-width:560px; display:block; background:#ffffff;' });

      // Subtle severity background zones
      var bands = [
        { from: 0, to: 1, fill: '#f8fafc' },
        { from: 1, to: 2, fill: '#f0f9ff' },
        { from: 2, to: 3, fill: '#fffbeb' },
        { from: 3, to: 4, fill: '#fef2f2' }
      ];
      bands.forEach(function (b) {
        svg.appendChild(svgEl('rect', {
          x: xOf(b.from), y: padT, width: xOf(b.to) - xOf(b.from), height: ph, fill: b.fill
        }));
      });

      // Vertical gridlines & top X-axis labels (Wear Extent)
      for (var s = 0; s <= 4; s++) {
        var gx = xOf(s);
        svg.appendChild(svgEl('line', {
          x1: gx, y1: padT, x2: gx, y2: padT + ph,
          stroke: s === 0 ? '#475569' : '#cbd5e1', 'stroke-width': s === 0 ? '1.5' : '1',
          'stroke-dasharray': s === 0 ? '' : '3,3'
        }));
        svg.appendChild(svgEl('text', {
          x: gx, y: padT - 10, 'text-anchor': 'middle', 'font-size': '10.5', 'font-weight': '600', fill: '#334155'
        }, SEV[s]));
      }

      // Horizontal gridlines & left Y-axis labels (Depth ORKB)
      var stepFt = maxDepth <= 2500 ? 500 : (maxDepth <= 6000 ? 1000 : 1500);
      for (var dFt = 0; dFt <= maxDepth; dFt += stepFt) {
        var gy = yOf(dFt);
        svg.appendChild(svgEl('line', {
          x1: padL, y1: gy, x2: padL + pw, y2: gy,
          stroke: dFt === 0 ? '#475569' : '#e2e8f0', 'stroke-width': dFt === 0 ? '1.5' : '1'
        }));
        svg.appendChild(svgEl('text', {
          x: padL - 8, y: gy + 3.5, 'text-anchor': 'end', 'font-size': '10', fill: '#475569'
        }, dFt + ' ft'));
      }

      // Axis titles
      svg.appendChild(svgEl('text', {
        x: padL + pw / 2, y: H - 8, 'text-anchor': 'middle', 'font-size': '11', 'font-weight': '700', fill: '#0f2544'
      }, 'Extent of Wear (' + compSel.options[compSel.selectedIndex].text + ')'));
      svg.appendChild(svgEl('text', {
        x: 14, y: padT + ph / 2, 'text-anchor': 'middle', 'font-size': '11', 'font-weight': '700', fill: '#0f2544',
        transform: 'rotate(-90 14 ' + (padT + ph / 2) + ')'
      }, 'Depth (ft ORKB)'));

      // Outer border
      svg.appendChild(svgEl('rect', {
        x: padL, y: padT, width: pw, height: ph, fill: 'none', stroke: '#94a3b8', 'stroke-width': '1'
      }));

      // Plot each visible run as a side-view step profile (oldest first so newest draws on top)
      var activeRuns = runsWithDepth.map(function (w, idx) { return { w: w, idx: idx }; })
        .filter(function (o) { return visible[o.w.run_date]; })
        .reverse();

      activeRuns.forEach(function (entry) {
        var w = entry.w, idx = entry.idx;
        var col = PALETTE[idx % PALETTE.length];
        // Slight horizontal offset so identical runs don't completely mask each other
        var jitter = ((idx % 5) - 2) * 2.2;
        var ivs = w.intervals.slice().sort(function (a, b) { return a.f_depth - b.f_depth; });

        // Build continuous step path for this run
        var pts = [];
        ivs.forEach(function (iv) {
          var rnk = rankFor(iv, metric);
          var x = Math.max(padL + 1, Math.min(padL + pw - 1, xOf(rnk) + (rnk > 0 ? jitter : 0)));
          pts.push(x.toFixed(1) + ',' + yOf(iv.f_depth).toFixed(1));
          pts.push(x.toFixed(1) + ',' + yOf(iv.t_depth).toFixed(1));
        });
        if (pts.length >= 2) {
          svg.appendChild(svgEl('polyline', {
            points: pts.join(' '), fill: 'none', stroke: col, 'stroke-width': '2.4',
            'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: '0.9'
          }));
        }

        // Interactive shaded strips + hover targets per interval
        ivs.forEach(function (iv) {
          var rnk = rankFor(iv, metric);
          var y1 = yOf(iv.f_depth), y2 = yOf(iv.t_depth);
          var h = Math.max(3, y2 - y1);
          var x = Math.max(padL + 1, Math.min(padL + pw - 1, xOf(rnk) + (rnk > 0 ? jitter : 0)));

          if (rnk > 0) {
            svg.appendChild(svgEl('rect', {
              x: padL, y: y1, width: Math.max(2, x - padL), height: h,
              fill: col, opacity: '0.08'
            }));
          }

          var hit = svgEl('line', {
            x1: x, y1: y1, x2: x, y2: y2,
            stroke: 'transparent', 'stroke-width': '12', style: 'cursor:pointer;'
          });
          var tipText = w.run_date + ' | ' + iv.f_depth + '–' + iv.t_depth + ' ft | ' + iv.rod + ' | ' + SEV[rnk];
          hit.appendChild(svgEl('title', {}, tipText));
          hit.addEventListener('mouseenter', function () {
            infoBox.textContent = '';
            var hdr = el('div', { style: 'font-weight:700; color:' + col + '; margin-bottom:4px;' }, 'Run: ' + w.run_date);
            infoBox.appendChild(hdr);
            infoBox.appendChild(el('div', { style: 'margin-bottom:3px;' }, 'Depth: ' + iv.f_depth + ' – ' + iv.t_depth + ' ft ORKB'));
            if (iv.rod) infoBox.appendChild(el('div', { style: 'margin-bottom:6px; color:#475569;' }, 'Rod: ' + iv.rod));
            var list = el('div', { style: 'border-top:1px solid #e2e8f0; padding-top:6px; line-height:1.55;' });
            ['Coupling', 'Shoulder', 'Neck', 'Body', 'Guides', 'Wearrate'].forEach(function (k) {
              var val = (iv.raw && iv.raw[k]) ? iv.raw[k] : '-';
              var row = el('div', { style: 'display:flex; justify-content:space-between;' });
              row.appendChild(el('span', { style: 'color:#64748b;' }, k === 'Wearrate' ? 'Wear Rate' : k));
              row.appendChild(el('strong', {}, val));
              list.appendChild(row);
            });
            infoBox.appendChild(list);
          });
          svg.appendChild(hit);
        });
      });

      svgWrap.appendChild(svg);
    }

    compSel.addEventListener('change', draw);
    draw();
    return box;
  }

  function render(d, c) {
    host.textContent = '';
    var det = el('details');
    try { if (localStorage.getItem('histPanelOpen') !== '0') det.setAttribute('open', ''); } catch (e) { det.setAttribute('open', ''); }
    det.addEventListener('toggle', function () { try { localStorage.setItem('histPanelOpen', det.open ? '1' : '0'); } catch (e) {} });

    var nWorn = (d.wear || []).filter(function (w) { return w.worn_count > 0; }).length;
    var nPumps = (d.pumps || []).length;
    var nRods = (d.rod_strings || []).length;
    det.appendChild(el('summary', {}, 'Well History — ' + d.well +
      '  (' + d.failures.length + ' workover' + (d.failures.length === 1 ? '' : 's') +
      ' · ' + nWorn + ' with wear · ' + nPumps + ' pump' + (nPumps === 1 ? '' : 's') +
      ' · ' + nRods + ' rod string' + (nRods === 1 ? '' : 's') + ')'));
    var body = el('div', { 'class': 'hist-body' });

    // 1. Failure History
    body.appendChild(el('h4', {}, 'Failure History'));
    if (!d.failures.length) body.appendChild(el('div', { 'class': 'hist-muted' }, 'No workover/failure records for this well yet.'));
    else {
      var heads = ['Run Date', 'W/O Date'].concat(d.columns.map(function (x) { return x.label; }));
      var cur = -1;
      var rows = d.failures.map(function (f, i) { if (c.run_date && f.run_date === c.run_date) cur = i; return [f.run_date, f.wo_date].concat(f.cells); });
      body.appendChild(table(heads, rows, cur));
    }

    // 2. Side-View Wear Erosion Profile + Wear History Table
    body.appendChild(el('h4', {}, 'Rod Wear History & Multi-Run Erosion Profile'));
    var chart = buildWearProfileChart(d.wear);
    if (chart) body.appendChild(chart);
    if (!d.wear.length) body.appendChild(el('div', { 'class': 'hist-muted' }, 'No rod wear records for this well yet.'));
    else {
      var wcur = -1;
      var wrows = d.wear.map(function (w, i) {
        if (c.run_date && w.run_date === c.run_date) wcur = i;
        var worn = el('div');
        if (!w.worn.length) worn.appendChild(el('span', { 'class': 'hist-muted' }, w.sections ? 'No wear recorded' : '—'));
        w.worn.forEach(function (s) {
          var line = el('div', { 'class': 'worn-line' });
          line.appendChild(sevBadge(s.rank)); line.appendChild(document.createTextNode(' #' + s.interval + ' ' + s.rod + (s.depth ? ' [' + s.depth + ' ft]' : '') + ' — ' + s.detail));
          worn.appendChild(line);
        });
        var cm = el('span', { title: w.comments }, w.comments.length > 70 ? w.comments.slice(0, 70) + '…' : w.comments);
        return [w.run_date, w.worn_count + ' / ' + w.sections, w.worn_count ? sevBadge(w.worst) : sevBadge(0), worn, w.pitting, cm];
      });
      body.appendChild(table(['Run Date', 'Worn / Sections', 'Worst', 'Worn Intervals', 'Pitting', 'General Comments'], wrows, wcur));
    }

    // 3. Downhole Pump History
    body.appendChild(el('h4', {}, 'Downhole Pump History'));
    if (!d.pumps || !d.pumps.length) {
      body.appendChild(el('div', { 'class': 'hist-muted' }, 'No downhole pump records for this well yet.'));
    } else {
      var pcur = -1;
      var prows = d.pumps.map(function (p, i) {
        if (c.run_date && p.run_date === c.run_date) pcur = i;
        var acts = el('div', { style: 'white-space:nowrap;' });
        var q = '?well=' + encodeURIComponent(d.well) + '&run_date=' + encodeURIComponent(p.run_date);
        acts.appendChild(linkBadge('Pump', '/pumps' + q, 'badge-blue'));
        if (p.has_teardown) acts.appendChild(linkBadge('Tear Down', '/reports/view/rptpumpreport' + q, 'badge-green'));
        return [
          p.run_date, p.pump_sn, p.designation, p.vendor, p.clearance, p.cond,
          p.start_date, p.failure_date, p.lifetime ? (p.lifetime + ' d') : '—', p.insp_cond, acts
        ];
      });
      body.appendChild(table(
        ['Run Date', 'Pump SN', 'API Designation', 'Vendor', 'Clearance', 'Cond.', 'Start', 'Failure', 'Lifetime', 'Insp. Cond.', 'Links'],
        prows, pcur
      ));
    }

    // 4. Rod String History
    body.appendChild(el('h4', {}, 'Rod String History'));
    if (!d.rod_strings || !d.rod_strings.length) {
      body.appendChild(el('div', { 'class': 'hist-muted' }, 'No rod string records for this well yet.'));
    } else {
      var rcur = -1;
      var rrows = d.rod_strings.map(function (r, i) {
        if (c.run_date && r.run_date === c.run_date) rcur = i;
        var acts = el('div', { style: 'white-space:nowrap;' });
        var q = '?well=' + encodeURIComponent(d.well) + '&run_date=' + encodeURIComponent(r.run_date);
        acts.appendChild(linkBadge('Rods', '/rods' + q, 'badge-blue'));
        acts.appendChild(linkBadge('Sketch', '/reports/view/rptwellboresketch' + q, 'badge-green'));
        return [
          r.run_date, r.rig, r.rotator, r.tapers,
          r.total_count ? (r.total_count + ' rods') : '—',
          r.total_length ? (r.total_length + ' ft') : '—',
          r.weight_air ? (r.weight_air + ' lbs') : '—',
          r.intake || '—',
          acts
        ];
      });
      body.appendChild(table(
        ['Run Date', 'RIH Rig', 'Rotator', 'Rod String Tapers', 'Total Rods', 'Length', 'Air Wt', 'Pump Intake (Below Pump)', 'Links'],
        rrows, rcur
      ));
    }

    det.appendChild(body);
    host.appendChild(det);
  }

  function load() {
    var c = ctx();
    if (c.well === lastWell) return;
    lastWell = c.well;
    host.textContent = '';
    if (!c.well) return;
    var url = '/api/well-history?well=' + encodeURIComponent(c.well);
    var fetcher = window.SROfflineCache
      ? window.SROfflineCache.cachedFetchJson(url)
      : fetch(url).then(function (r) { return r.json(); });
    fetcher
      .then(function (d) { if (d && d.ok) render(d, c); })
      .catch(function () {});
  }
  function later() { clearTimeout(timer); timer = setTimeout(load, 250); }
  document.addEventListener('change', function (e) { if (e.target.matches && e.target.matches(WELL_SEL)) later(); });
  load();
})();
