(function () {
  // ── formatters ──────────────────────────────────────────────────────────
  const fmtMoney = (v) => {
    if (v == null || isNaN(v)) return "$0.00";
    const sign = v < 0 ? "-" : "";
    return sign + "$" + Math.abs(v).toFixed(2);
  };
  const fmtSigned = (v) => {
    if (v == null || isNaN(v)) return "$0.00";
    return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
  };
  const fmtPct = (v) => (v == null || isNaN(v) ? "0.00%" : (v * 100).toFixed(2) + "%");
  const fmtPrice = (v) => (v == null ? "—" : Number(v).toFixed(4));
  const fmtSpot  = (v) => (v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 }));
  const fmtTime  = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  };
  const fmtDuration = (sec) => {
    if (sec == null) return "0s";
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  };

  // ── status helpers ───────────────────────────────────────────────────────
  const STATUS_LABEL = {
    idle: "Inactivo", loading_market: "Cargando…", watching: "Vigilando",
    holding: "Holding", traded: "Operado", hedged: "Hedgeado",
    sold: "Vendido", error: "Error", mm_placed: "MM colocado",
  };
  const STATUS_CLASS = {
    watching: "ok", traded: "ok", hedged: "ok", mm_placed: "ok",
    holding: "warn", loading_market: "warn", sold: "warn",
    error: "err",
  };

  const MARKET_ICONS = { btc: "₿", sol: "◎", eth: "Ξ" };
  const MARKET_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea" };

  // ── element refs ─────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);

  // ── render header ────────────────────────────────────────────────────────
  function renderHeader(s) {
    const c = s.config || {};
    const mb = $("mode-badge");
    if (mb) {
      mb.textContent = (c.mode || "paper").toUpperCase();
      mb.className = "badge " + (c.mode === "real" ? "real" : "paper");
    }

    const markets = s.markets || {};
    ["btc", "sol", "eth"].forEach((sym) => {
      const el = $(["ws-" + sym]);
      if (!el) return;
      const m = markets[sym];
      const ok = m && m.ws_connected;
      el.className = "badge " + (ok ? "ok" : "err");
    });

    // Spot prices row
    const sp = $("spot-prices");
    if (sp) {
      sp.innerHTML = Object.entries(markets).map(([sym, m]) =>
        `<div class="spot-item">
          <span class="spot-label" style="color:${MARKET_COLOR[sym]}">${sym.toUpperCase()}</span>
          <span class="spot-val">${fmtSpot(m.spot_price)}</span>
        </div>`
      ).join("");
    }
  }

  // ── render global KPIs ───────────────────────────────────────────────────
  function renderKpis(s) {
    const st = s.combined_stats || {};
    const bk = $("kpi-bankroll");
    if (bk) {
      bk.textContent = fmtMoney(st.bankroll);
      bk.className = "kpi-value" + (st.resolved_pnl > 0 ? " up" : st.resolved_pnl < 0 ? " down" : "");
    }
    const cashEl = $("kpi-cash");
    if (cashEl) cashEl.textContent = "efectivo " + fmtMoney(st.available_cash);

    const pnl = $("kpi-pnl");
    if (pnl) {
      pnl.textContent = fmtSigned(st.resolved_pnl);
      pnl.className = "kpi-value" + (st.resolved_pnl > 0 ? " up" : st.resolved_pnl < 0 ? " down" : "");
    }
    const roi = $("kpi-roi");
    if (roi) roi.textContent = "ROI " + fmtPct(st.roi);

    const wr = $("kpi-winrate");
    if (wr) wr.textContent = fmtPct(st.win_rate);
    const wl = $("kpi-wl");
    if (wl) wl.textContent = `${st.wins || 0}V / ${st.losses || 0}D`;

    const tr = $("kpi-trades");
    if (tr) tr.textContent = st.trades || 0;
    const op = $("kpi-open");
    if (op) op.textContent = `${st.open || 0} abiertas`;

    const up = $("kpi-uptime");
    if (up) up.textContent = fmtDuration(st.uptime_seconds);
    const upt = $("uptime");
    if (upt) upt.textContent = fmtDuration(st.uptime_seconds);
  }

  // ── render one market panel ──────────────────────────────────────────────
  function renderMarket(sym, m) {
    const panel = $("panel-" + sym);
    if (!panel) return;

    const icon  = MARKET_ICONS[sym] || sym.toUpperCase();
    const color = MARKET_COLOR[sym] || "#888";
    const stCls = STATUS_CLASS[m.bot_status] || "";
    const stLbl = STATUS_LABEL[m.bot_status] || m.bot_status;
    const wsCls = m.ws_connected ? "ok" : "err";
    const slug  = m.current_slug || "—";
    const ttl   = m.seconds_remaining != null ? fmtDuration(m.seconds_remaining) : "—";
    const up    = m.last_up_price;
    const down  = m.last_down_price;
    const st    = m.stats || {};
    const pnlCls = st.resolved_pnl >= 0 ? "pnl-pos" : "pnl-neg";

    panel.innerHTML = `
      <div class="market-header">
        <div class="market-title">
          <span class="market-icon" style="color:${color}">${icon}</span>
          <div>
            <div class="market-name">${m.label} <span class="muted" style="font-size:12px;font-weight:400;">(${sym.toUpperCase()})</span></div>
            <div class="mono muted" style="font-size:11px;">${slug}</div>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <span class="badge ${stCls}">${stLbl}</span>
          <span class="badge ${wsCls}">WS ●</span>
          <span class="mono muted" style="font-size:12px;">${fmtSpot(m.spot_price)}</span>
        </div>
      </div>

      <div class="prices" style="margin-top:14px;">
        <div class="price up">
          <span class="side">▲ SUBE</span>
          <span class="value">${up != null ? up.toFixed(4) : "—"}</span>
          <div class="bar"><div class="bar-fill" style="width:${up != null ? (up * 100).toFixed(1) : 0}%"></div></div>
        </div>
        <div class="price down">
          <span class="side">▼ BAJA</span>
          <span class="value">${down != null ? down.toFixed(4) : "—"}</span>
          <div class="bar"><div class="bar-fill" style="width:${down != null ? (down * 100).toFixed(1) : 0}%"></div></div>
        </div>
      </div>

      <div class="market-meta">
        <span>⏱ <strong>${ttl}</strong></span>
        <span>Trades <strong>${st.trades || 0}</strong></span>
        <span>Victoria <strong>${(st.win_rate * 100 || 0).toFixed(0)}%</strong></span>
        <span class="${pnlCls}">P&L <strong>${fmtSigned(st.resolved_pnl || 0)}</strong></span>
      </div>
    `;
  }

  // ── render combined trades table ─────────────────────────────────────────
  function renderTrades(s) {
    const tbody = $("trades-body");
    if (!tbody) return;

    const markets = s.markets || {};
    let allTrades = [];
    for (const [sym, m] of Object.entries(markets)) {
      (m.trades || []).forEach((t) => allTrades.push({ ...t, _sym: sym }));
    }
    allTrades.sort((a, b) => b.opened_at - a.opened_at);
    allTrades = allTrades.slice(0, 100);

    if (allTrades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">Sin operaciones — esperando datos.</td></tr>`;
      return;
    }

    const MKT_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea" };
    tbody.innerHTML = allTrades.map((t) => {
      const pnlCls  = t.pnl == null ? "" : t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
      const pnlText = t.pnl == null ? "—" : fmtSigned(t.pnl);
      const typeTag = t.is_hedge
        ? `<span class="tag hedge">HEDGE</span>`
        : `<span class="tag initial">INIT</span>`;
      const stratTag = t.strategy === "mm"
        ? `<span class="tag strat-mm">MM</span>`
        : `<span class="tag strat-trigger">TRG</span>`;
      const sym = t._sym;
      const symColor = MKT_COLOR[sym] || "#aaa";
      return `<tr${t.is_hedge ? ' class="row-hedge"' : ""}>
        <td><span style="color:${symColor};font-weight:700;font-size:12px;">${sym.toUpperCase()}</span></td>
        <td>#${t.id} ${typeTag}</td>
        <td>${stratTag}</td>
        <td class="mono" style="font-size:11px;">${t.window_slug}</td>
        <td><span class="tag ${t.side.toLowerCase()}">${t.side}</span></td>
        <td>${Number(t.price).toFixed(4)}</td>
        <td>${Number(t.shares).toFixed(2)}</td>
        <td>${fmtMoney(t.cost)}</td>
        <td><span class="tag ${t.status}">${t.status}</span></td>
        <td class="${pnlCls}">${pnlText}</td>
      </tr>`;
    }).join("");
  }

  // ── render combined activity log ─────────────────────────────────────────
  function renderLog(s) {
    const logEl = $("log");
    if (!logEl || !s.log) return;
    const items = [...s.log].reverse();
    const MKT_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea" };
    logEl.innerHTML = items.map((entry) => {
      const mkt = entry.market;
      const mktTag = mkt
        ? `<span class="mkt-tag" style="background:${MKT_COLOR[mkt]}22;color:${MKT_COLOR[mkt]};padding:1px 5px;border-radius:4px;font-size:10px;font-weight:700;">${mkt.toUpperCase()}</span>`
        : "";
      return `<li>
        <span class="ts">${fmtTime(entry.t)}</span>
        ${mktTag}
        <span class="level ${entry.level}">${entry.level}</span>
        <span class="msg"></span>
      </li>`;
    }).join("");
    // Set text content safely for msg spans
    logEl.querySelectorAll("li").forEach((li, i) => {
      const msgEl = li.querySelector(".msg");
      if (msgEl && items[i]) msgEl.textContent = items[i].message;
    });
  }

  // ── main render ──────────────────────────────────────────────────────────
  function render(s) {
    renderHeader(s);
    renderKpis(s);
    const markets = s.markets || {};
    for (const [sym, m] of Object.entries(markets)) {
      renderMarket(sym, m);
    }
    renderTrades(s);
    renderLog(s);
  }

  // ── poll ─────────────────────────────────────────────────────────────────
  async function poll() {
    try {
      const resp = await fetch("state", { cache: "no-store" });
      if (!resp.ok) return;
      const s = await resp.json();
      render(s);
    } catch (_) { /* silently retry */ }
  }

  poll();
  setInterval(poll, 1000);
})();
