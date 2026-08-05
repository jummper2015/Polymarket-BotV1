/* Trades table: filters, 25-row pagination and CSV export.
 *
 * Fetches /api/trades on demand rather than riding along with the 1 Hz /state
 * poll. The old table rewrote its entire tbody every second, which threw away
 * text selection and scroll position while you were reading it.
 */
(function () {
  const { $, STRAT_LABELS, fmtMoney, fmtSigned, fmtPrice, fmtDateTime, esc } = window.SS;

  const PER_PAGE = 25;
  // Long enough that typing a slug doesn't fire a query per keystroke, short
  // enough that the table doesn't feel stuck.
  const DEBOUNCE_MS = 300;

  let page = 1;
  let debounce = null;
  // Rows only change when a window resolves (~5 min), so a slow background
  // refresh is enough to keep it current without fighting the user.
  const AUTO_REFRESH_MS = 30000;

  const FILTER_INPUTS = {
    strategy: "f-strategy",
    symbol: "f-symbol",
    status: "f-status",
    direction: "f-direction",
    mode: "f-mode",
    from: "f-from",
    to: "f-to",
    q: "f-q",
  };

  function currentFilters() {
    const out = {};
    Object.entries(FILTER_INPUTS).forEach(([key, id]) => {
      const el = $(id);
      if (!el) return;
      const value = (el.value || "").trim();
      if (value && value !== "all") out[key] = value;
    });
    return out;
  }

  function queryString(extra) {
    const params = new URLSearchParams(currentFilters());
    Object.entries(extra || {}).forEach(([k, v]) => params.set(k, v));
    return params.toString();
  }

  function statusPill(status) {
    const label = { won: "Ganada", lost: "Perdida", open: "Abierta" }[status] || status;
    return `<span class="ss-pill ${esc(status)}">${esc(label)}</span>`;
  }

  function renderRows(items) {
    const tbody = $("trades-body");
    if (!tbody) return;

    if (!items.length) {
      tbody.innerHTML =
        '<tr><td colspan="13" class="ss-empty">Sin operaciones con estos filtros.</td></tr>';
      return;
    }

    tbody.innerHTML = items.map((t) => {
      const pnlCls = (t.pnl || 0) >= 0 ? "ss-pos" : "ss-neg";
      const stratCls = t.strategy === "ss_fade" ? "strat-fade" : "strat-trend";
      const sideCls = t.direction === "UP" ? "side-up" : "side-down";
      // The DB path returns entry_price; the in-memory fallback returns price.
      const price = t.entry_price != null ? t.entry_price : t.price;
      return `
        <tr>
          <td>${t.id}</td>
          <td class="text-muted">${esc(fmtDateTime(t.opened_at))}</td>
          <td>${esc(String(t.symbol || "btc").toUpperCase())}</td>
          <td class="${stratCls}">${esc(STRAT_LABELS[t.strategy] || t.strategy)}</td>
          <td class="${sideCls}">${esc(t.direction)}</td>
          <td>$${fmtPrice(price)}</td>
          <td>${esc(t.shares)}</td>
          <td>${fmtMoney(t.cost)}</td>
          <td>×${Number(t.multiplier || 1).toFixed(2)}</td>
          <td>${statusPill(t.status)}</td>
          <td class="${pnlCls}">${t.pnl != null ? fmtSigned(t.pnl) : "—"}</td>
          <td class="text-muted">${esc(t.resolution_source || "—")}</td>
          <td class="text-muted text-truncate" style="max-width:160px"
              title="${esc(t.note)}">${esc(t.note)}</td>
        </tr>`;
    }).join("");
  }

  function renderPager(data) {
    const pager = $("trades-pager");
    const count = $("trades-count");

    if (count) {
      const first = data.total === 0 ? 0 : (data.page - 1) * data.per_page + 1;
      const last = Math.min(data.page * data.per_page, data.total);
      count.textContent = data.total
        ? `Mostrando ${first}–${last} de ${data.total} operaciones`
        : "Sin operaciones";
    }

    if (!pager) return;
    if (data.pages <= 1) { pager.innerHTML = ""; return; }

    const item = (label, target, opts = {}) => {
      const cls = ["page-item"];
      if (opts.active) cls.push("active");
      if (opts.disabled) cls.push("disabled");
      return `<li class="${cls.join(" ")}">
                <a class="page-link" href="#" data-page="${target}">${label}</a>
              </li>`;
    };

    // Window of pages around the current one — a full list is unusable once
    // the bot has run for a few days.
    const around = 2;
    const start = Math.max(1, data.page - around);
    const end = Math.min(data.pages, data.page + around);

    let html = item("«", data.page - 1, { disabled: data.page <= 1 });
    if (start > 1) {
      html += item("1", 1);
      if (start > 2) html += '<li class="page-item disabled"><span class="page-link">…</span></li>';
    }
    for (let p = start; p <= end; p++) html += item(p, p, { active: p === data.page });
    if (end < data.pages) {
      if (end < data.pages - 1) html += '<li class="page-item disabled"><span class="page-link">…</span></li>';
      html += item(data.pages, data.pages);
    }
    html += item("»", data.page + 1, { disabled: data.page >= data.pages });

    pager.innerHTML = html;
  }

  async function load() {
    try {
      const resp = await fetch("/api/trades?" + queryString({ page, per_page: PER_PAGE }),
                               { cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const data = await resp.json();
      // A stale page number (filter narrowed the set) would show an empty table.
      if (data.page > data.pages && data.pages >= 1) {
        page = data.pages;
        return load();
      }
      renderRows(data.items || []);
      renderPager(data);
    } catch (err) {
      const tbody = $("trades-body");
      if (tbody) {
        tbody.innerHTML =
          '<tr><td colspan="13" class="ss-empty">No se pudieron cargar las operaciones.</td></tr>';
      }
    }
  }

  function reload() {
    page = 1;   // any filter change invalidates the current page
    load();
  }

  /* The strategy and asset dropdowns are filled from /state, not written into
   * the template: a strategy added to the registry, or a symbol added to
   * SS_SYMBOLS, has to be filterable without editing the HTML. One call at
   * startup is enough — neither list changes without a restart. */
  async function populateFilters() {
    try {
      const resp = await fetch("/state", { cache: "no-store" });
      if (!resp.ok) return;
      const s = await resp.json();

      const fill = (id, options) => {
        const el = $(id);
        if (!el || !options.length) return;
        const keep = el.value;
        el.innerHTML =
          el.options[0].outerHTML +
          options.map(([v, label]) =>
            `<option value="${esc(v)}">${esc(label)}</option>`).join("");
        el.value = keep;   // a reload mustn't silently widen the filter
      };

      fill("f-strategy", (s.strategies || []).map((d) => [d.id, d.name || d.id]));
      fill("f-symbol", (s.symbols || []).map((sym) => [sym, String(sym).toUpperCase()]));
    } catch (_) {
      // Leaving the dropdowns with just "Todas" is a degraded filter, not a
      // broken table — the rows load either way.
    }
  }

  // ── wiring ──
  Object.values(FILTER_INPUTS).forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (el.tagName === "SELECT" || el.type === "date") {
      el.addEventListener("change", reload);
    } else {
      el.addEventListener("input", () => {
        clearTimeout(debounce);
        debounce = setTimeout(reload, DEBOUNCE_MS);
      });
    }
  });

  const clear = $("f-clear");
  if (clear) {
    clear.addEventListener("click", () => {
      Object.values(FILTER_INPUTS).forEach((id) => {
        const el = $(id);
        if (!el) return;
        el.value = el.tagName === "SELECT" ? "all" : "";
      });
      reload();
    });
  }

  const pager = $("trades-pager");
  if (pager) {
    pager.addEventListener("click", (ev) => {
      const link = ev.target.closest("a[data-page]");
      if (!link) return;
      ev.preventDefault();
      const target = parseInt(link.dataset.page, 10);
      if (!isNaN(target) && target >= 1) { page = target; load(); }
    });
  }

  // Export honours whatever filters are on screen — downloading everything
  // when the table shows a filtered view would be a surprise.
  const exportBtn = $("trades-export");
  if (exportBtn) {
    exportBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.location.href = "/api/trades.csv?" + queryString();
    });
  }

  populateFilters();
  load();
  setInterval(() => { if (!document.hidden) load(); }, AUTO_REFRESH_MS);
})();
