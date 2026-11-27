/* Dynamic Page Layout & Custom Fields Engine (Option A).
   - Fetches /api/page-layout?page=...&well=...&run_date=...
   - Applies Admin sort_order, custom labels, and hide/show rules to built-in fields in each section.
   - Injects custom fields directly into their target section at their exact sort_order.
   - Auto-saves custom field values when the user submits the page's main form. */
(function () {
  var host = document.getElementById('cf-panel');
  if (!host) return;
  var page = host.getAttribute('data-page');
  var WELL_SEL = 'input[name="WellName"],select[name="WellName"],input[name="well_name"],select[name="well_name"],input[name="well"],select[name="well"]';
  var DATE_SEL = 'input[name="WellRunDate"],select[name="WellRunDate"],input[name="run_date"],select[name="run_date"]';
  var timer = null, lastKey = null, inputs = {}, formHooked = false;

  // Inject helper style so Admin hiding never collides with Completion Type visibility
  var st = document.createElement('style');
  st.textContent = '.cf-admin-hidden { display: none !important; }';
  document.head.appendChild(st);

  function ctx() {
    var q = new URLSearchParams(location.search);
    var w = (document.querySelector(WELL_SEL) || {}).value || q.get('well') || '';
    var d = (document.querySelector(DATE_SEL) || {}).value || q.get('run_date') || '';
    return { well: w.trim(), run_date: d.trim() };
  }

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text) e.textContent = text;
    return e;
  }

  function buildCustomFieldNode(f) {
    var inp;
    if (f.type === 'dropdown') {
      inp = el('select');
      inp.appendChild(el('option', { value: '' }, '— select —'));
      (f.options || []).forEach(function (o) { inp.appendChild(el('option', { value: o }, o)); });
      if (f.value && (f.options || []).indexOf(f.value) < 0) {
        inp.appendChild(el('option', { value: f.value }, f.value + ' (not in list)'));
      }
    } else {
      inp = el('input', { type: f.type === 'number' ? 'number' : (f.type === 'date' ? 'date' : 'text') });
      if (f.type === 'number') inp.setAttribute('step', 'any');
      if (f.placeholder) inp.setAttribute('placeholder', f.placeholder);
    }
    inp.value = f.value || f.default_value || '';
    inputs[f.key] = inp;

    var wrap = el('div', { 'class': 'field cf-injected', 'data-cf-key': f.key });
    wrap.appendChild(el('label', {}, f.label + (f.required ? ' *' : '')));
    wrap.appendChild(inp);
    var err = el('small', { style: 'color:#b91c1c;' });
    err.id = 'cf-err-' + f.key;
    wrap.appendChild(err);
    return wrap;
  }

  function findSectionGrid(form, sec) {
    if (!form) return null;
    for (var i = 0; i < sec.items.length; i++) {
      var it = sec.items[i];
      if (it.kind === 'builtin') {
        var ctrl = form.querySelector('[name="' + it.name.replace(/"/g, '\\"') + '"]');
        if (ctrl) {
          var grid = ctrl.closest('.form-grid');
          if (grid) return grid;
        }
      }
    }
    return null;
  }

  function setStatus(msg, ok) {
    var s = document.getElementById('cf-status');
    if (!s) return;
    s.textContent = msg;
    s.style.color = ok ? '#15803d' : '#b91c1c';
  }

  function saveCustomValues(c) {
    var keys = Object.keys(inputs);
    if (!keys.length) return Promise.resolve({ ok: true, saved: 0 });
    var vals = {};
    keys.forEach(function (k) {
      vals[k] = inputs[k].value;
      var e = document.getElementById('cf-err-' + k);
      if (e) e.textContent = '';
    });
    return fetch('/api/custom-values', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: page, well: c.well, run_date: c.run_date, values: vals })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok) {
        setStatus('Saved ' + res.j.saved + ' custom value(s) ✔', true);
        return { ok: true, saved: res.j.saved };
      }
      if (res.j && res.j.errors) {
        for (var k in res.j.errors) {
          var e = document.getElementById('cf-err-' + k);
          if (e) e.textContent = res.j.errors[k];
        }
      }
      setStatus((res.j && res.j.error) || 'Please fix highlighted custom fields.', false);
      return { ok: false };
    }).catch(function () {
      setStatus('Save failed (network).', false);
      return { ok: false };
    });
  }

  function hookMainForm(form) {
    if (!form || formHooked) return;
    formHooked = true;
    form.addEventListener('submit', function (ev) {
      if (!Object.keys(inputs).length) return;
      var c = ctx();
      if (!c.well || !c.run_date) return;
      ev.preventDefault();
      saveCustomValues(c).then(function (res) {
        if (res.ok) form.submit();
      });
    });
  }

  function applyLayout(data, c) {
    // Clean previously injected nodes
    document.querySelectorAll('.cf-injected').forEach(function (n) { n.remove(); });
    host.textContent = '';
    inputs = {};

    var form = document.querySelector('main form[method="post"]');
    hookMainForm(form);

    var bottomCustom = [];
    var totalCustom = 0;

    (data.sections || []).forEach(function (sec) {
      if (sec.key === '__bottom__') {
        sec.items.forEach(function (it) { if (it.kind === 'custom') bottomCustom.push(it); });
        return;
      }
      var grid = findSectionGrid(form, sec);
      if (!grid) {
        sec.items.forEach(function (it) { if (it.kind === 'custom') bottomCustom.push(it); });
        return;
      }

      // Reorder built-in fields and inject custom fields directly into this section's .form-grid
      sec.items.forEach(function (it) {
        if (it.kind === 'builtin') {
          var inGrid = true;
          var ctrl = grid.querySelector('[name="' + it.name.replace(/"/g, '\\"') + '"]');
          if (!ctrl && form) {
            ctrl = form.querySelector('[name="' + it.name.replace(/"/g, '\\"') + '"]');
            inGrid = false;
          }
          if (!ctrl) return;
          var fieldDiv = ctrl.closest('.field');
          if (!fieldDiv) return;
          var lbl = fieldDiv.querySelector('label');
          if (lbl && it.label) lbl.textContent = it.label;
          if (ctrl.tagName === 'INPUT' || ctrl.tagName === 'TEXTAREA') {
            if (it.placeholder) {
              ctrl.setAttribute('placeholder', it.placeholder);
            } else {
              ctrl.removeAttribute('placeholder');
            }
          }
          if (it.default_value && !ctrl.value) {
            ctrl.value = it.default_value;
          }
          fieldDiv.classList.toggle('cf-admin-hidden', !it.active);
          if (inGrid) grid.appendChild(fieldDiv);
        } else if (it.kind === 'custom') {
          totalCustom += 1;
          var node = buildCustomFieldNode(it);
          if (!c.well || !c.run_date) {
            inputs[it.key].disabled = true;
            inputs[it.key].title = 'Select a Well Name and Run Date first';
          }
          grid.appendChild(node);
        }
      });
    });

    // Render any "__bottom__" custom fields (or a compact save bar if custom fields exist)
    if (bottomCustom.length) {
      totalCustom += bottomCustom.length;
      var card = el('div', { 'class': 'card' });
      card.appendChild(el('h3', {}, 'Additional Custom Fields'));
      if (!c.well || !c.run_date) {
        card.appendChild(el('p', { style: 'font-size:12.5px;color:#475569;' }, 'Select a Well Name and Run Date to enter custom fields.'));
      } else {
        var bar = el('div', { 'class': 'filter-bar', style: 'flex-wrap:wrap;align-items:flex-end;' });
        bottomCustom.forEach(function (f) { bar.appendChild(buildCustomFieldNode(f)); });
        card.appendChild(bar);
        var btn = el('button', { 'class': 'btn btn-success', type: 'button' }, 'Save Custom Fields');
        btn.addEventListener('click', function () { saveCustomValues(ctx()); });
        card.appendChild(btn);
        card.appendChild(el('span', { id: 'cf-status', style: 'margin-left:12px;font-size:12.5px;' }));
      }
      host.appendChild(card);
    } else if (totalCustom > 0 && c.well && c.run_date) {
      // Subtle status strip when all custom fields are inline inside sections
      var strip = el('div', { style: 'margin-top:8px; font-size:12px; color:#475569;' });
      strip.appendChild(el('span', { id: 'cf-status' }, 'Custom fields save automatically when you click Save above.'));
      host.appendChild(strip);
    }
  }

  function load() {
    var c = ctx(), key = c.well + '|' + c.run_date;
    if (key === lastKey) return;
    lastKey = key;
    var u = '/api/page-layout?page=' + encodeURIComponent(page)
      + '&well=' + encodeURIComponent(c.well)
      + '&run_date=' + encodeURIComponent(c.run_date);
    var fetcher = window.SROfflineCache
      ? window.SROfflineCache.cachedFetchJson(u)
      : fetch(u).then(function (r) { return r.json(); });
    fetcher.then(function (d) {
      if (d && d.ok) applyLayout(d, c);
    }).catch(function () {});
  }

  function later() { clearTimeout(timer); timer = setTimeout(load, 200); }
  document.addEventListener('change', function (e) { if (e.target.matches && e.target.matches(WELL_SEL + ',' + DATE_SEL)) later(); });
  document.addEventListener('input', function (e) { if (e.target.matches && e.target.matches(WELL_SEL + ',' + DATE_SEL)) later(); });
  document.addEventListener('change', function (e) { if (e.target.id && /custom_date/.test(e.target.id)) setTimeout(later, 50); });
  load();
})();