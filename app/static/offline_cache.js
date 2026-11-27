/* Offline-capable PWA support: Service Worker registration, IndexedDB data cache,
   "Offline Mode - Cached Data" indicator, and searchable cached well browser. */
(function () {
  var DB_NAME = 'sr_offline_db';
  var DB_VERSION = 1;
  var STORE_API = 'api_cache';
  var STORE_SNAPSHOT = 'snapshot';
  var LS_PREFIX = 'sr_off_';
  var isOfflineMode = !navigator.onLine;
  var lastServedFromCache = false;
  var dbPromise = null;

  // Register Service Worker at root scope
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }

  function openIDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve) {
      if (!('indexedDB' in window)) {
        resolve(null);
        return;
      }
      try {
        var req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = function (e) {
          var db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_API)) {
            db.createObjectStore(STORE_API, { keyPath: 'key' });
          }
          if (!db.objectStoreNames.contains(STORE_SNAPSHOT)) {
            db.createObjectStore(STORE_SNAPSHOT, { keyPath: 'key' });
          }
        };
        req.onsuccess = function (e) { resolve(e.target.result); };
        req.onerror = function () { resolve(null); };
      } catch (err) {
        resolve(null);
      }
    });
    return dbPromise;
  }

  function idbPut(storeName, record) {
    return openIDB().then(function (db) {
      if (!db) {
        try {
          localStorage.setItem(LS_PREFIX + storeName + '_' + record.key, JSON.stringify(record));
        } catch (e) {}
        return;
      }
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(storeName, 'readwrite');
          tx.objectStore(storeName).put(record);
          tx.oncomplete = function () { resolve(); };
          tx.onerror = function () { resolve(); };
        } catch (e) {
          resolve();
        }
      });
    });
  }

  function idbGet(storeName, key) {
    return openIDB().then(function (db) {
      if (!db) {
        try {
          var raw = localStorage.getItem(LS_PREFIX + storeName + '_' + key);
          return raw ? JSON.parse(raw) : null;
        } catch (e) {
          return null;
        }
      }
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(storeName, 'readonly');
          var req = tx.objectStore(storeName).get(key);
          req.onsuccess = function () { resolve(req.result || null); };
          req.onerror = function () { resolve(null); };
        } catch (e) {
          resolve(null);
        }
      });
    });
  }

  function idbGetAll(storeName) {
    return openIDB().then(function (db) {
      if (!db) return [];
      return new Promise(function (resolve) {
        try {
          var tx = db.transaction(storeName, 'readonly');
          var req = tx.objectStore(storeName).getAll();
          req.onsuccess = function () { resolve(req.result || []); };
          req.onerror = function () { resolve([]); };
        } catch (e) {
          resolve([]);
        }
      });
    });
  }

  function ensureBannerStyles() {
    if (document.getElementById('sr-offline-styles')) return;
    var st = document.createElement('style');
    st.id = 'sr-offline-styles';
    st.textContent = [
      '#sr-offline-banner { display:none; background:#fffbeb; color:#92400e; border-bottom:2px solid #f59e0b; padding:9px 18px; font-size:12.5px; font-weight:600; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; z-index:900; }',
      '#sr-offline-banner.visible { display:flex; }',
      'body.dark-mode #sr-offline-banner { background:#451a03; color:#fde68a; border-bottom-color:#d97706; }',
      '#sr-offline-drawer { display:none; margin:12px 24px 0; padding:14px 16px; background:#ffffff; border:1px solid #f59e0b; border-radius:8px; box-shadow:0 6px 18px rgba(15,23,42,0.08); }',
      '#sr-offline-drawer.open { display:block; }',
      'body.dark-mode #sr-offline-drawer { background:#1e293b; border-color:#d97706; color:#f1f5f9; }',
      '.sr-off-dot { width:9px; height:9px; border-radius:50%; background:#d97706; display:inline-block; margin-right:6px; }',
      '.sr-off-search-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }',
      '.sr-off-search-row input { flex:1; min-width:220px; padding:7px 10px; border:1px solid #cbd5e1; border-radius:6px; font-size:13px; }'
    ].join('\n');
    document.head.appendChild(st);
  }

  function ensureBannerDOM() {
    ensureBannerStyles();
    if (document.getElementById('sr-offline-banner')) return;

    var banner = document.createElement('div');
    banner.id = 'sr-offline-banner';
    banner.className = 'no-print';

    var left = document.createElement('div');
    left.style.cssText = 'display:flex; align-items:center; gap:6px; flex-wrap:wrap;';
    var dot = document.createElement('span');
    dot.className = 'sr-off-dot';
    var label = document.createElement('span');
    label.id = 'sr-offline-label';
    label.textContent = 'Offline Mode - Cached Data';
    var meta = document.createElement('span');
    meta.id = 'sr-offline-meta';
    meta.style.cssText = 'font-weight:400; font-size:11.5px; opacity:0.9; margin-left:6px;';
    left.appendChild(dot);
    left.appendChild(label);
    left.appendChild(meta);

    var right = document.createElement('div');
    right.style.cssText = 'display:flex; align-items:center; gap:8px;';
    var browseBtn = document.createElement('button');
    browseBtn.type = 'button';
    browseBtn.className = 'btn btn-outline';
    browseBtn.style.cssText = 'padding:4px 10px; font-size:11.5px;';
    browseBtn.textContent = 'Search Cached Records';
    browseBtn.addEventListener('click', toggleOfflineDrawer);
    right.appendChild(browseBtn);

    banner.appendChild(left);
    banner.appendChild(right);

    var drawer = document.createElement('div');
    drawer.id = 'sr-offline-drawer';
    drawer.className = 'no-print';

    var mainEl = document.querySelector('.main');
    var contentEl = document.querySelector('.content') || document.getElementById('sw-offline-explorer');
    if (mainEl && contentEl) {
      mainEl.insertBefore(banner, contentEl);
      mainEl.insertBefore(drawer, contentEl);
    } else {
      document.body.insertBefore(drawer, document.body.firstChild);
      document.body.insertBefore(banner, drawer);
    }
  }

  function updateBannerState(offline, cacheTimestamp) {
    isOfflineMode = !!offline;
    if (offline) lastServedFromCache = true;
    ensureBannerDOM();
    var banner = document.getElementById('sr-offline-banner');
    var meta = document.getElementById('sr-offline-meta');
    if (!banner) return;

    var show = !navigator.onLine || isOfflineMode;
    banner.classList.toggle('visible', show);
    if (meta) {
      if (cacheTimestamp) {
        meta.textContent = '(Last synced: ' + new Date(cacheTimestamp).toLocaleString() + ')';
      } else {
        idbGet(STORE_SNAPSHOT, 'latest').then(function (snap) {
          if (snap && snap.updatedAt && meta) {
            meta.textContent = '(Last synced: ' + new Date(snap.updatedAt).toLocaleString() + ')';
          }
        });
      }
    }
  }

  function renderOfflineExplorer(container) {
    container.textContent = '';
    var title = document.createElement('div');
    title.style.cssText = 'display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;';
    var h = document.createElement('strong');
    h.textContent = 'Offline Mode - Cached Data Explorer (Wells, Workovers, Pumps & Rods)';
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn btn-outline';
    closeBtn.style.cssText = 'padding:3px 9px; font-size:11px;';
    closeBtn.textContent = 'Close';
    closeBtn.addEventListener('click', function () { container.classList.remove('open'); });
    title.appendChild(h);
    if (container.id === 'sr-offline-drawer') title.appendChild(closeBtn);
    container.appendChild(title);

    var searchRow = document.createElement('div');
    searchRow.className = 'sr-off-search-row';
    var inp = document.createElement('input');
    inp.type = 'search';
    inp.placeholder = 'Search cached Well Name, Pump SN, Failure Reason, or Date...';
    searchRow.appendChild(inp);
    container.appendChild(searchRow);

    var resultsBox = document.createElement('div');
    container.appendChild(resultsBox);

    Promise.all([idbGet(STORE_SNAPSHOT, 'latest'), idbGetAll(STORE_API)]).then(function (pair) {
      var snap = pair[0];
      var apiEntries = pair[1] || [];
      var cachedHistoryWells = [];
      apiEntries.forEach(function (entry) {
        if (entry.key && entry.key.indexOf('/api/well-history?well=') === 0 && entry.data && entry.data.well) {
          cachedHistoryWells.push(entry.data.well);
        }
      });

      function renderRows(q) {
        resultsBox.textContent = '';
        var term = (q || '').trim().toLowerCase();
        if (!snap && !cachedHistoryWells.length) {
          var empty = document.createElement('p');
          empty.style.cssText = 'font-size:12.5px; color:#64748b; margin:4px 0;';
          empty.textContent = 'No cached records stored yet. Connect once and load the Dashboard or any well to cache records for offline browsing.';
          resultsBox.appendChild(empty);
          return;
        }

        var wells = (snap && snap.wells) ? snap.wells : [];
        var workovers = (snap && snap.workovers) ? snap.workovers : [];
        var pumps = (snap && snap.pumps) ? snap.pumps : [];

        var matchedWells = wells.filter(function (w) {
          return !term || (w.WellName || '').toLowerCase().indexOf(term) >= 0 || (w.notes || '').toLowerCase().indexOf(term) >= 0;
        }).slice(0, 30);

        var matchedWO = workovers.filter(function (r) {
          if (!term) return true;
          return [r.WellName, r.WellRunDate, r.WOverDate, r.Wellfailurereason, r['W/Oreason'], r.RIGNAMErods]
            .some(function (v) { return String(v || '').toLowerCase().indexOf(term) >= 0; });
        }).slice(0, 20);

        var matchedPumps = pumps.filter(function (p) {
          if (!term) return true;
          return [p.WellName, p.WellRunDate, p.PumpSN, p.Pumpdesignation, p.Vendor]
            .some(function (v) { return String(v || '').toLowerCase().indexOf(term) >= 0; });
        }).slice(0, 20);

        var info = document.createElement('div');
        info.style.cssText = 'font-size:11.5px; color:#64748b; margin-bottom:8px;';
        info.textContent = 'Cached snapshot: ' + wells.length + ' wells · ' + workovers.length + ' workovers · ' + pumps.length + ' pumps · ' + cachedHistoryWells.length + ' full well histories';
        resultsBox.appendChild(info);

        if (matchedWells.length) {
          var wWrap = document.createElement('div');
          wWrap.style.cssText = 'display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px;';
          matchedWells.forEach(function (w) {
            var a = document.createElement('a');
            var hasHist = cachedHistoryWells.indexOf(w.WellName) >= 0;
            a.className = 'badge ' + (hasHist ? 'badge-green' : 'badge-blue');
            a.style.textDecoration = 'none';
            a.href = '/wells?well=' + encodeURIComponent(w.WellName);
            a.textContent = w.WellName + (hasHist ? ' ★' : '');
            a.title = hasHist ? 'Full well history cached offline' : 'Cached well';
            wWrap.appendChild(a);
          });
          resultsBox.appendChild(wWrap);
        }

        if (matchedWO.length) {
          var tblWrap = document.createElement('div');
          tblWrap.className = 'table-wrap';
          tblWrap.style.maxHeight = '220px';
          var tbl = document.createElement('table');
          var thead = document.createElement('thead');
          var hr = document.createElement('tr');
          ['Well', 'Run Date', 'W/O Date', 'Failure Reason', 'Rig', 'Status'].forEach(function (hTxt) {
            var th = document.createElement('th');
            th.textContent = hTxt;
            hr.appendChild(th);
          });
          thead.appendChild(hr);
          tbl.appendChild(thead);

          var tb = document.createElement('tbody');
          matchedWO.forEach(function (r) {
            var tr = document.createElement('tr');
            [r.WellName, r.WellRunDate, r.WOverDate, r.Wellfailurereason || r['W/Oreason'] || '—', r.RIGNAMErods || '—', r.WellStatus || '—']
              .forEach(function (val) {
                var td = document.createElement('td');
                td.textContent = val || '—';
                tr.appendChild(td);
              });
            tb.appendChild(tr);
          });
          tbl.appendChild(tb);
          tblWrap.appendChild(tbl);
          resultsBox.appendChild(tblWrap);
        }

        if (matchedPumps.length && term) {
          var pTitle = document.createElement('div');
          pTitle.style.cssText = 'font-size:12px; font-weight:700; margin:10px 0 4px;';
          pTitle.textContent = 'Matching Cached Pumps (' + matchedPumps.length + ')';
          resultsBox.appendChild(pTitle);
          var pWrap = document.createElement('div');
          pWrap.className = 'table-wrap';
          pWrap.style.maxHeight = '160px';
          var pt = document.createElement('table');
          var ptHead = document.createElement('thead');
          var phr = document.createElement('tr');
          ['Well', 'Run Date', 'Pump SN', 'Designation', 'Vendor', 'Lifetime (d)'].forEach(function (hTxt) {
            var th = document.createElement('th');
            th.textContent = hTxt;
            phr.appendChild(th);
          });
          ptHead.appendChild(phr);
          pt.appendChild(ptHead);
          var ptb = document.createElement('tbody');
          matchedPumps.forEach(function (p) {
            var tr = document.createElement('tr');
            [p.WellName, p.WellRunDate, p.PumpSN || '—', p.Pumpdesignation || '—', p.Vendor || '—', p.PumpLifetime || '—']
              .forEach(function (val) {
                var td = document.createElement('td');
                td.textContent = val || '—';
                tr.appendChild(td);
              });
            ptb.appendChild(tr);
          });
          pt.appendChild(ptb);
          pWrap.appendChild(pt);
          resultsBox.appendChild(pWrap);
        }
      }

      inp.addEventListener('input', function () { renderRows(inp.value); });
      renderRows('');
    });
  }

  function toggleOfflineDrawer() {
    ensureBannerDOM();
    var drawer = document.getElementById('sr-offline-drawer');
    if (!drawer) return;
    var opening = !drawer.classList.contains('open');
    drawer.classList.toggle('open', opening);
    if (opening) renderOfflineExplorer(drawer);
  }

  function cachedFetchJson(url) {
    if (!navigator.onLine) {
      return idbGet(STORE_API, url).then(function (entry) {
        if (entry && entry.data) {
          updateBannerState(true, entry.updatedAt);
          return entry.data;
        }
        throw new Error('Offline and no cached data available for ' + url);
      });
    }
    return fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        idbPut(STORE_API, { key: url, data: data, updatedAt: Date.now() });
        if (lastServedFromCache && navigator.onLine) {
          lastServedFromCache = false;
          updateBannerState(false);
        }
        return data;
      })
      .catch(function (err) {
        return idbGet(STORE_API, url).then(function (entry) {
          if (entry && entry.data) {
            updateBannerState(true, entry.updatedAt);
            return entry.data;
          }
          throw err;
        });
      });
  }

  function syncSnapshot(force) {
    if (!navigator.onLine) return;
    var path = window.location.pathname;
    if (path === '/login' || path === '/setup') return;
    try {
      var last = parseInt(sessionStorage.getItem('sr_snap_ts') || '0', 10);
      if (!force && last && (Date.now() - last) < 120000) return;
    } catch (e) {}

    fetch('/api/offline-snapshot')
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (data && data.ok) {
          data.key = 'latest';
          data.updatedAt = Date.now();
          idbPut(STORE_SNAPSHOT, data);
          try { sessionStorage.setItem('sr_snap_ts', String(Date.now())); } catch (e) {}
        }
      })
      .catch(function () {});
  }

  window.SROfflineCache = {
    cachedFetchJson: cachedFetchJson,
    syncSnapshot: syncSnapshot,
    toggleOfflineDrawer: toggleOfflineDrawer,
    isOffline: function () { return !navigator.onLine || isOfflineMode; }
  };

  window.addEventListener('online', function () {
    lastServedFromCache = false;
    updateBannerState(false);
    syncSnapshot(true);
  });

  window.addEventListener('offline', function () {
    updateBannerState(true);
  });

  // Prevent confusing browser error pages when submitting POST forms while offline
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || (form.getAttribute('method') || '').toLowerCase() !== 'post') return;
    if (!navigator.onLine) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      var errBox = document.getElementById('global-form-err');
      var msg = 'Offline Mode: Saving or modifying records requires an active internet connection.';
      if (errBox) {
        errBox.textContent = msg;
        errBox.style.display = 'block';
        window.scrollTo({ top: 0, behavior: 'smooth' });
      } else {
        alert(msg);
      }
    }
  }, true);

  document.addEventListener('DOMContentLoaded', function () {
    updateBannerState(!navigator.onLine);
    var swHost = document.getElementById('sw-offline-explorer');
    if (swHost) renderOfflineExplorer(swHost);
    setTimeout(function () { syncSnapshot(false); }, 600);
  });
})();
