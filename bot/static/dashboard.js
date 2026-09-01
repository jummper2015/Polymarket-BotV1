/* Streak Snapper — live dashboard.
 *
 * Polls /state once a second and paints the header, KPIs, martingale, prices,
 * order book and log. Trades live in trades.js (paged, on demand) and the
 * Chart.js panels in charts.js — both consume the helpers exported on
 * window.SS at the bottom of this file.
 */
(function () {
  const $ = (id) => document.getElementById(id);

  /* Read the palette from CSS instead of repeating hex values here. The old
   * version hardcoded them, so changing a colour meant editing two files and
   * they drifted apart. */
  const css = getComputedStyle(document.documentElement);
  const color = (name, fallback) =>
    (css.getPropertyValue(name) || "").trim() || fallback;

  const COLORS = {
    up:    color("--ss-up", "#00c292"),
    down:  color("--ss-down", "#fb9678"),
    ok:    color("--ss-ok", "#00c292"),
    warn:  color("--ss-warn", "#fec107"),
    err:   color("--ss-err", "#e46a76"),
    box:   color("--ss-box", "#7b68ee"),
    cfd:   color("--ss-cfd", "#03a9f4"),
    muted: color("--ss-muted", "#7b8794"),
    border: color("--ss-border", "#e4e9f0"),
  };

  // ── formatters ──
  const fmtMoney = (v) => {
    if (v == null || isNaN(v)) return "$0.00";
    return (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2);
  };
  const fmtSigned = (v) => {
    if (v == null || isNaN(v)) return "$0.00";
    return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
  };
  const fmtPct = (v) => (v == null || isNaN(v) ? "—" : (v * 100).toFixed(1) + "%");
  const fmtPrice = (v) => (v == null ? "—" : Number(v).toFixed(4));
  const fmtSpot = (v) =>
    v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  const fmtDuration = (sec) => {
    if (sec == null) return "0s";
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  };
  const fmtTime = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  };
  const fmtDateTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    return d.toLocaleString("es-ES",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  // Anything from the DB can end up in innerHTML; notes are free text.
  const esc = (v) =>
    String(v == null ? "" : v).replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const STRAT_LABELS = { box_builder: "Box Builder", coin_flip_dog: "Coin Flip Dog" };
  const STRAT_COLORS = { box_builder: COLORS.box, coin_flip_dog: COLORS.cfd };

  /* Which market the live panel describes. The trader runs one thread per
   * symbol in SS_SYMBOLS, so prices, order book and window countdown belong to
   * exactly one of them — mixing them would show a BTC price under an ETH
   * window. Remembered across reloads; a stale name just falls back to the
   * first symbol the backend reports. */
  let activeSymbol = null;
  try {
    activeSymbol = localStorage.getItem("ss_symbol");
  } catch (_) { /* private mode */ }

  function setActiveSymbol(symbol) {
    activeSymbol = symbol;
    try {
      localStorage.setItem("ss_symbol", symbol);
    } catch (_) { /* private mode */ }
    refresh();
  }

  // ── main render loop ──
  let failures = 0;
  let registry = [];   // /state.strategies, for names and colours

  async function refresh() {
    try {
      const url = activeSymbol
        ? "/state?symbol=" + encodeURIComponent(activeSymbol)
        : "/state";
      const resp = await fetch(url, { cache: "no-store" });
      if (resp.status === 401) {
        // Session expired — the page is now useless, so send the user to login.
        window.location.href = "/login";
        return;
      }
      if (!resp.ok) throw new Error("HTTP " + resp.status);

      const s = await resp.json();
      failures = 0;
      setConnectionError(false);

      // The backend decides which symbol it actually served (an unknown one
      // falls back), so follow it instead of trusting what we asked for.
      if (s.symbol) activeSymbol = s.symbol;
      registry = s.strategies || registry;
      registry.forEach((d) => {
        STRAT_LABELS[d.id] = d.name || d.id;
      });

      renderSymbolTabs(s);
      renderHeader(s);
      renderKpis(s);
      renderMartingale(s);
      renderStatus(s);
      renderPrices(s);
      renderOrderBook(s);
      renderPriceChart(s);
      renderStrategyMetrics(s);
      renderSymbolMetrics(s);
      renderSkips(s);
      renderLog(s);
    } catch (err) {
      /* The old code swallowed this, so a dead backend looked like a frozen but
       * healthy dashboard. Surface it after a couple of misses so a single
       * blip doesn't flash a warning. */
      if (++failures >= 3) setConnectionError(true);
    }
  }

  function setConnectionError(broken) {
    const ws = $("ws-badge");
    if (ws && broken) {
      ws.className = "ss-badge err";
      ws.textContent = "● Sin conexión";
      ws.title = "No se puede contactar con el bot";
    }
  }

  function renderHeader(s) {
    const c = s.config || {};
    const mb = $("mode-badge");
    if (mb) {
      mb.textContent = (c.mode || "paper").toUpperCase();
      mb.className = "ss-badge " + (c.mode === "real" ? "real" : "paper");
    }
    const ws = $("ws-badge");
    if (ws) {
      const ok = s.status && s.status.ws_connected;
      ws.className = "ss-badge " + (ok ? "ok" : "err");
      ws.textContent = ok ? "● WS Live" : "● WS Off";
      ws.title = "";
    }
    const sp = $("spot-btc");
    if (sp) {
      sp.innerHTML = `BTC <span class="ss-spot-val">${fmtSpot(s.status && s.status.spot_price)}</span>`;
    }
    renderChainlink(s.status && s.status.chainlink);
  }

  /* Chainlink TWAP badge. Freshness is the point: a frozen feed still reports a
   * "latest" value, so age is what says whether to trust it — and it's the
   * number CL_TWAP_STALE_SECONDS gets tuned from. */
  function renderChainlink(cl) {
    const el = $("cl-badge");
    if (!el) return;

    if (!cl || !cl.enabled) {
      // Off by config is a normal state, not a failure. Don't cry wolf.
      el.className = "ss-badge off";
      el.textContent = "● CL Off";
      el.title = "Chainlink TWAP desactivado (CL_TWAP_ENABLED=false)";
      return;
    }
    if (cl.subscribe_failed) {
      el.className = "ss-badge warn";
      el.textContent = "● CL n/d";
      el.title = "Topics TWAP aún no disponibles en el relay — el bot opera igual";
      return;
    }

    const w = (cl.windows || {})["30"] || (cl.windows || {})["60"];
    if (!w) {
      el.className = "ss-badge warn";
      el.textContent = "● CL …";
      el.title = "Conectado, sin ticks todavía";
      return;
    }

    const div = typeof cl.divergence === "number"
      ? (cl.divergence * 100).toFixed(3) + "%" : "n/d";
    el.className = "ss-badge " + (w.stale ? "warn" : "ok");
    el.textContent = "● CL " + (w.age_s != null ? w.age_s.toFixed(1) + "s" : "live");
    el.title = `TWAP ${fmtSpot(w.value)} · edad ${w.age_s}s · `
             + `relay ${w.relay_lag_s}s · divergencia spot ${div}`;
  }

  function renderKpis(s) {
    const st = s.stats || {};
    const resolved = (st.wins || 0) + (st.losses || 0);
    const signCls = (v) => (v > 0 ? " ss-pos" : v < 0 ? " ss-neg" : "");

    const pnl = $("kpi-pnl");
    if (pnl) {
      pnl.textContent = fmtSigned(st.resolved_pnl);
      pnl.className = "ss-kpi-value" + signCls(st.resolved_pnl);
    }
    const roi = $("kpi-roi");
    if (roi) roi.textContent = "ROI " + (resolved > 0 ? fmtPct(st.roi) : "—");

    const wr = $("kpi-winrate");
    if (wr) {
      wr.textContent = resolved > 0 ? fmtPct(st.win_rate) : "—";
      wr.className = "ss-kpi-value" +
        (resolved === 0 ? "" : st.win_rate >= 0.5 ? " ss-pos" : " ss-neg");
    }
    const wl = $("kpi-wl");
    if (wl) wl.textContent = resolved > 0 ? `${st.wins}V / ${st.losses}D` : "Sin resueltas";

    const tr = $("kpi-trades");
    if (tr) tr.textContent = st.trades || 0;
    const op = $("kpi-open");
    if (op) op.textContent = `${st.open || 0} abiertas`;

    /* Available vs committed. Money leaves the account when a position opens,
     * but resolved_pnl only moves when it closes — so a single "bankroll"
     * figure looked unchanged right after entering a trade. */
    const av = $("kpi-available");
    if (av) av.textContent = fmtMoney(st.available != null ? st.available : st.bankroll);
    const cm = $("kpi-committed");
    if (cm) {
      const committed = st.committed || 0;
      cm.textContent = committed > 0
        ? fmtMoney(committed) + " comprometido"
        : "sin posiciones abiertas";
    }

    const bk = $("kpi-bankroll");
    if (bk) {
      bk.textContent = fmtMoney(st.bankroll);
      bk.className = "ss-kpi-value" + signCls(st.resolved_pnl);
    }
    const bkBase = $("kpi-bankroll-base");
    if (bkBase) bkBase.textContent = "base " + fmtMoney(st.starting_bankroll);

    const upt = $("uptime");
    if (upt) upt.textContent = fmtDuration(st.uptime_seconds);
  }

  function renderMartingale(s) {
    // None of the active strategies use martingale. Show live enabled state
    // and key params for all four registered strategies.
    const cfg = s.config || {};
    const strats = s.strategies || [];

    const bbDesc  = strats.find((d) => d.id === "box_builder");
    const cfdDesc = strats.find((d) => d.id === "coin_flip_dog");
    const taDesc  = strats.find((d) => d.id === "temporal_arb");
    const nrcDesc = strats.find((d) => d.id === "near_res");

    const set = (id, value) => { const el = $(id); if (el) el.textContent = value; };

    // Box Builder card
    const bbOn = cfg.bb_enabled !== false && bbDesc && bbDesc.enabled;
    set("bb-status", bbOn ? "activa ✓" : "apagada");
    const bbCard = $("bb-card");
    if (bbCard) bbCard.classList.toggle("ss-strategy-off", !bbOn);
    set("bb-shares", cfg.bb_shares_per_leg != null ? cfg.bb_shares_per_leg + " sh" : "—");
    set("bb-cap", cfg.bb_bid_sum_cap != null ? "$" + cfg.bb_bid_sum_cap : "—");

    // Coin-Flip Dog card
    const cfdOn = cfg.cfd_enabled !== false && cfdDesc && cfdDesc.enabled;
    set("cfd-status", cfdOn ? "activa ✓" : "apagada");
    const cfdCard = $("cfd-card");
    if (cfdCard) cfdCard.classList.toggle("ss-strategy-off", !cfdOn);
    set("cfd-bet", cfg.cfd_base_bet != null ? "$" + cfg.cfd_base_bet : "—");
    const minL = cfg.cfd_entry_min_left;
    const maxL = cfg.cfd_entry_max_left;
    set("cfd-window", (minL != null && maxL != null) ? `T-${maxL}..T-${minL}s` : "—");

    // Temporal Arb card
    const taOn = cfg.ta_enabled !== false && taDesc && taDesc.enabled;
    set("ta-status", taOn ? "activa ✓" : "apagada");
    const taCard = $("ta-card");
    if (taCard) taCard.classList.toggle("ss-strategy-off", !taOn);
    set("ta-threshold", cfg.ta_cheap_threshold != null ? "≤ $" + cfg.ta_cheap_threshold : "—");
    set("ta-cap", cfg.ta_complete_cap != null ? "≤ $" + cfg.ta_complete_cap : "—");

    // Near-Resolution Capture card
    const nrcOn = cfg.nrc_enabled !== false && nrcDesc && nrcDesc.enabled;
    set("nrc-status", nrcOn ? "activa ✓" : "apagada");
    const nrcCard = $("nrc-card");
    if (nrcCard) nrcCard.classList.toggle("ss-strategy-off", !nrcOn);
    const nrcMinL = cfg.nrc_min_entry_left;
    const nrcMaxL = cfg.nrc_max_entry_left;
    set("nrc-window", (nrcMinL != null && nrcMaxL != null) ? `T-${nrcMaxL}..T-${nrcMinL}s` : "—");
    set("nrc-shares", cfg.nrc_shares != null ? cfg.nrc_shares + " sh" : "—");
  }

  function renderStatus(s) {
    const st = s.status || {};
    const set = (id, value) => { const el = $(id); if (el) el.textContent = value; };

    set("status-text", st.bot_status || "idle");
    set("status-msg", st.bot_message || "");
    set("status-slug", st.current_slug || "—");
    set("status-ttl", st.seconds_remaining != null ? fmtDuration(st.seconds_remaining) : "—");
    // Build active-strategy label from the live registry instead of a hardcoded string.
    const activeStrats = (s.strategies || [])
      .filter(d => d.enabled)
      .map(d => d.id)
      .join(" + ");
    set("status-mode", activeStrats || "ninguna");
    set("status-btc", fmtSpot(st.spot_price));

    const dot = $("status-dot");
    if (dot) {
      const state = st.bot_status === "error" ? "error"
        : (st.bot_status === "watching" || st.bot_status === "holding") ? "running"
        : "waiting";
      dot.className = "ss-status-dot " + state;
    }
  }

  function renderPrices(s) {
    const p = s.prices || {};
    const ph = s.price_history || {};
    const set = (id, value) => { const el = $(id); if (el) el.textContent = value; };

    set("price-up-mid", fmtPrice(p.up_mid));
    set("price-down-mid", fmtPrice(p.down_mid));
    set("price-up-bid", fmtPrice(p.up_bid));
    set("price-up-ask", fmtPrice(p.up_ask));
    set("price-down-bid", fmtPrice(p.down_bid));
    set("price-down-ask", fmtPrice(p.down_ask));

    const sparkEl = $("price-sparklines");
    if (sparkEl && ph.up && ph.up.length > 1) {
      const upPts = ph.up.map((d) => d.p).slice(-120);
      const dnPts = (ph.down || []).map((d) => d.p).slice(-120);
      const row = (label, pts, c) => `
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="small fw-bold" style="color:${c};width:44px">${label}</span>
          <svg viewBox="0 0 200 30" class="flex-grow-1">${sparkline(pts, c)}</svg>
          <span class="small text-muted">${fmtPrice(pts[pts.length - 1])}</span>
        </div>`;
      sparkEl.innerHTML = row("UP", upPts, COLORS.up) + row("DOWN", dnPts, COLORS.down);
    }
  }

  function renderOrderBook(s) {
    const el = $("order-book");
    if (!el) return;
    const ob = s.order_book || {};

    const sides = [
      { key: "UP", label: "UP", c: COLORS.up },
      { key: "DOWN", label: "DOWN", c: COLORS.down },
    ];

    if (!sides.some(({ key }) => ob[key])) {
      el.innerHTML = '<div class="ss-empty">Esperando libro de órdenes...</div>';
      return;
    }

    el.innerHTML = sides.map(({ key, label, c }) => {
      const entry = ob[key];
      if (!entry) {
        return `<div class="col-6"><div class="ss-ob-side">
                  <div class="ss-ob-title" style="color:${c}">${label}</div>
                  <div class="ss-empty">Sin datos</div></div></div>`;
      }
      // Bids descend (best first), asks ascend (best first).
      const bids = [...(entry.bids || [])]
        .sort((a, b) => Number(b.price) - Number(a.price)).slice(0, 5);
      const asks = [...(entry.asks || [])]
        .sort((a, b) => Number(a.price) - Number(b.price)).slice(0, 5);

      const bestBid = bids.length ? Number(bids[0].price) : null;
      const bestAsk = asks.length ? Number(asks[0].price) : null;
      const spread = bestBid != null && bestAsk != null ? bestAsk - bestBid : null;

      const rows = (levels, cls) => levels.length
        ? levels.map((l) => `<div class="ss-ob-row"><span class="${cls}">${fmtPrice(l.price)}</span>` +
            `<span class="text-muted">${Number(l.size || 0).toFixed(0)}</span></div>`).join("")
        : '<div class="ss-empty">—</div>';

      return `
        <div class="col-6">
          <div class="ss-ob-side">
            <div class="ss-ob-title" style="color:${c}">${label}</div>
            <div class="row g-1">
              <div class="col-6"><div class="small fw-bold" style="color:${COLORS.up}">Bids</div>${rows(bids, "bid")}</div>
              <div class="col-6"><div class="small fw-bold" style="color:${COLORS.down}">Asks</div>${rows(asks, "ask")}</div>
            </div>
            <div class="d-flex justify-content-between small text-muted mt-1">
              <span>Spread ${spread != null ? spread.toFixed(4) : "—"}</span>
              <span>Vol ${Number(entry.volume || 0).toFixed(0)}</span>
            </div>
          </div>
        </div>`;
    }).join("");
  }

  function sparkline(points, c) {
    if (!points || points.length < 2) return "";
    const min = Math.min(...points), max = Math.max(...points);
    const range = max - min || 1;
    const px = (i) => (i / (points.length - 1)) * 200;
    const py = (v) => 28 - ((v - min) / range) * 24;
    const pts = points.map((v, i) => `${px(i)},${py(v)}`).join(" ");
    return `<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="1.5" ` +
           `stroke-linejoin="round" stroke-linecap="round"/>`;
  }

  /* Hand-built SVG rather than Chart.js: this redraws every second and the
   * window resets it every 5 minutes, so a chart instance would be churned
   * constantly for a two-series line. */
  function renderPriceChart(s) {
    const ph = s.price_history || {};
    const el = $("price-chart-container");
    if (!el) return;

    const upPts = (ph.up || []).map((d) => d.p);
    const dnPts = (ph.down || []).map((d) => d.p);

    if (upPts.length < 2 && dnPts.length < 2) {
      el.innerHTML = '<div class="ss-empty">Esperando datos del WebSocket...</div>';
      return;
    }

    const allPts = [...upPts, ...dnPts];
    const minP = Math.min(...allPts), maxP = Math.max(...allPts);
    const range = maxP - minP || 0.02;
    const pad = range * 0.1;
    const yMin = Math.max(0, minP - pad);
    const yMax = Math.min(1, maxP + pad);

    const w = 780, h = 200, padLR = 48, padTB = 20;
    const pw = w - padLR * 2, phH = h - padTB * 2;
    const xScale = (i, total) => padLR + (i / Math.max(total - 1, 1)) * pw;
    const yScale = (v) => padTB + phH - ((v - yMin) / (yMax - yMin || 0.01)) * phH;

    const makePath = (pts, c) => {
      if (pts.length < 2) return "";
      const d = pts.map((v, i) =>
        `${i === 0 ? "M" : "L"}${xScale(i, pts.length).toFixed(1)},${yScale(v).toFixed(1)}`
      ).join(" ");
      return `<path d="${d}" fill="none" stroke="${c}" stroke-width="2" ` +
             `stroke-linejoin="round" stroke-linecap="round"/>`;
    };

    const grid = [];
    for (let i = 0; i <= 4; i++) {
      const val = yMin + ((yMax - yMin) * i) / 4;
      const y = yScale(val).toFixed(1);
      grid.push(`<line x1="${padLR}" x2="${w - padLR}" y1="${y}" y2="${y}" stroke="${COLORS.border}" stroke-width="1"/>`);
      grid.push(`<text x="${padLR - 6}" y="${y}" text-anchor="end" dominant-baseline="middle" ` +
                `fill="${COLORS.muted}" font-size="9">${val.toFixed(3)}</text>`);
    }

    const labels = [];
    const totalPts = Math.max(upPts.length, dnPts.length);
    const step = Math.max(1, Math.floor(totalPts / 5));
    for (let i = 0; i < totalPts; i += step) {
      const ts = ((ph.up || [])[i] || (ph.down || [])[i] || {}).t;
      labels.push(`<text x="${xScale(i, totalPts).toFixed(1)}" y="${h - 4}" text-anchor="middle" ` +
                  `fill="${COLORS.muted}" font-size="9">${ts ? fmtTime(ts) : ""}</text>`);
    }

    const upMid = upPts.length ? fmtPrice(upPts[upPts.length - 1]) : "—";
    const dnMid = dnPts.length ? fmtPrice(dnPts[dnPts.length - 1]) : "—";

    el.innerHTML = `
      <div class="d-flex gap-3 small text-muted mb-1">
        <span><span class="d-inline-block rounded-circle" style="width:8px;height:8px;background:${COLORS.up}"></span> UP: ${upMid}</span>
        <span><span class="d-inline-block rounded-circle" style="width:8px;height:8px;background:${COLORS.down}"></span> DOWN: ${dnMid}</span>
      </div>
      <svg viewBox="0 0 ${w} ${h}">
        ${grid.join("")}${labels.join("")}
        ${makePath(upPts, COLORS.up)}${makePath(dnPts, COLORS.down)}
      </svg>`;

    const timer = $("chart-timer");
    if (timer && s.status && s.status.seconds_remaining != null) {
      timer.textContent = fmtDuration(s.status.seconds_remaining) + " restante";
    }
  }

  /* Per-strategy cards, one per registered strategy plus anything with history
   * in the table. The list used to be hard-coded here, so a strategy added to
   * the bot traded invisibly until someone remembered this file. */
  function renderStrategyMetrics(s) {
    const el = $("strat-metrics");
    if (!el) return;
    const ss = s.strategy_stats || {};

    const scope = $("strat-metrics-scope");
    if (scope) {
      scope.textContent = s.symbol
        ? `histórico de ${String(s.symbol).toUpperCase()}`
        : "histórico completo";
    }

    // Registry order first; then any id the DB knows about and the registry
    // doesn't — a retired strategy still owns its history.
    const keys = registry.map((d) => d.id);
    Object.keys(ss).forEach((k) => { if (!keys.includes(k)) keys.push(k); });

    const width = keys.length >= 3 ? "col-lg-4 col-md-6" : "col-md-6";

    el.innerHTML = keys.map((key) => {
      const desc = registry.find((d) => d.id === key);
      const label = desc ? desc.name : STRAT_LABELS[key] || key;
      const c = STRAT_COLORS[key] || COLORS.muted;
      const st = ss[key] || { trades: 0, wins: 0, losses: 0, win_rate: 0, pnl: 0, roi: 0 };
      const resolved = (st.wins || 0) + (st.losses || 0);
      const pnlCls = (st.pnl || 0) >= 0 ? "ss-pos" : "ss-neg";
      const roiCls = (st.roi || 0) >= 0 ? "ss-pos" : "ss-neg";
      const wrCls = resolved > 0 ? ((st.win_rate || 0) >= 0.5 ? "ss-pos" : "ss-neg") : "";
      // A registered strategy that is switched off still shows its history —
      // greyed, so nobody reads a frozen P&L as a live one.
      const off = desc && desc.enabled === false;
      const row = (k, v, cls) =>
        `<div class="ss-mart-row"><span>${k}</span><span class="ss-mart-val ${cls || ""}">${v}</span></div>`;
      return `
        <div class="${width}">
          <div class="ss-mart${off ? " ss-strategy-off" : ""}" style="border-left-color:${c}">
            <div class="ss-mart-head" style="color:${c}">
              ${esc(label)}${off ? ' <span class="ss-badge warn">apagada</span>' : ""}
            </div>
            ${row("Trades", st.trades)}
            ${row("V / D", `${st.wins}V / ${st.losses}D`)}
            ${row("Win Rate", resolved > 0 ? fmtPct(st.win_rate) : "—", wrCls)}
            ${row("P&amp;L", fmtSigned(st.pnl), pnlCls)}
            ${row("ROI", st.total_invested ? fmtPct(st.roi) : "—", roiCls)}
          </div>
        </div>`;
    }).join("");
  }

  /* The asset tabs. One market configured means no tab bar: SS_SYMBOLS defaults
   * to btc and a single-tab selector is just noise. */
  function renderSymbolTabs(s) {
    const el = $("symbol-tabs");
    if (!el) return;
    const symbols = s.symbols || [];
    if (symbols.length < 2) {
      el.style.display = "none";
      return;
    }
    el.style.display = "";
    el.innerHTML = symbols.map((sym) => {
      const active = sym === s.symbol ? " active" : "";
      return `<button type="button" class="ss-symbol-tab${active}" data-symbol="${esc(sym)}">
                ${esc(String(sym).toUpperCase())}
              </button>`;
    }).join("");
  }

  // Delegated so it survives every re-render of the tab bar.
  document.addEventListener("click", (ev) => {
    const tab = ev.target.closest ? ev.target.closest("[data-symbol]") : null;
    if (tab && tab.classList.contains("ss-symbol-tab")) {
      setActiveSymbol(tab.dataset.symbol);
    }
  });

  /* Per-asset totals. Always every market, never filtered by the tab: the point
   * is comparing BTC against ETH against SOL, which a filtered view can't do. */
  function renderSymbolMetrics(s) {
    const el = $("symbol-metrics");
    if (!el) return;
    const stats = s.symbol_stats || {};
    const keys = Object.keys(stats);

    if (!keys.length) {
      el.innerHTML = '<div class="col-12"><div class="ss-empty">Sin operaciones todavía</div></div>';
      return;
    }

    el.innerHTML = keys.map((sym) => {
      const st = stats[sym];
      const resolved = (st.wins || 0) + (st.losses || 0);
      const pnlCls = (st.pnl || 0) >= 0 ? "ss-pos" : "ss-neg";
      const row = (k, v, cls) =>
        `<div class="ss-mart-row"><span>${k}</span><span class="ss-mart-val ${cls || ""}">${v}</span></div>`;
      return `
        <div class="col-md-4">
          <div class="ss-mart${sym === s.symbol ? " ss-mart-active" : ""}">
            <div class="ss-mart-head">${esc(String(sym).toUpperCase())}</div>
            ${row("Trades", st.trades)}
            ${row("Win Rate", resolved > 0 ? fmtPct(st.win_rate) : "—")}
            ${row("P&amp;L", fmtSigned(st.pnl), pnlCls)}
            ${row("ROI", st.total_invested ? fmtPct(st.roi) : "—")}
          </div>
        </div>`;
    }).join("");
  }

  /* Windows a regime filter refused, by reason. The filters are all off by
   * default and only reach significance with live data, so the count of what
   * each one *would have* skipped is the measurement, not a footnote. */
  const SKIP_LABELS = {
    SKIP_HOURS: "Fuera de la franja horaria",
    SKIP_VOL: "Volatilidad fuera de banda",
    SKIP_RANGE: "Rango de 2h demasiado ancho",
    // Este no es un filtro de régimen: viene encendido. Ver ss_max_entry_age.
    SKIP_LATE: "Ventana demasiado avanzada",
    // Tampoco: el libro pedía más que el cap. Antes se dejaba una puja por
    // debajo del ask y se registraba como posición. Ver is_ask_above_cap.
    SKIP_ASK_ABOVE_CAP: "Ask por encima del cap",
    // Solo en modo real: la orden no llegó a enviarse.
    SKIP_ORDER_FAILED: "Orden rechazada por el CLOB",
    // Solo en modo real: se envió, no se llenó y se canceló. Antes de que se
    // verificara el llenado esto se registraba como posición.
    SKIP_NO_FILL: "Orden enviada sin llenar",
  };

  // Contadores que una estrategia publica sin operar. Spread-Harvest sale así
  // a propósito: una puja en reposo no se puede simular en paper, así que
  // primero mide cuántas ventanas serían cotizables. Ver spread_harvest.py.
  const OBS_LABELS = {
    SH_WINDOWS: "Ventanas observadas",
    SH_QUOTABLE: "Cotizables (moneda al aire + libro ancho)",
    SH_SKIP_COA: "Descartadas: el precio se fue del strike",
    SH_SKIP_SPREAD: "Descartadas: libro demasiado estrecho",
    SH_NO_DATA: "Sin strike o sin ATR",
  };

  function renderSkips(s) {
    const el = $("skip-metrics");
    if (!el) return;
    const entries = Object.entries(s.skips || {}).sort((a, b) => b[1] - a[1]);
    const obs = Object.entries(s.observations || {}).sort((a, b) => b[1] - a[1]);

    if (!entries.length && !obs.length) {
      el.innerHTML =
        '<div class="ss-empty">Ningún filtro ha descartado ventanas' +
        '<div class="ss-field-hint mt-1">Todos vienen apagados por defecto</div></div>';
      return;
    }

    let html = "";

    if (entries.length) {
      const total = entries.reduce((acc, [, n]) => acc + n, 0);
      html +=
        entries.map(([reason, count]) => `
          <div class="ss-mart-row">
            <span>${esc(SKIP_LABELS[reason] || reason)}</span>
            <span class="ss-mart-val">${count}</span>
          </div>`).join("") +
        `<div class="ss-mart-row mt-1" style="border-top:1px solid var(--ss-border)">
           <span><strong>Total</strong></span><span class="ss-mart-val">${total}</span>
         </div>`;
    }

    if (obs.length) {
      // La tasa es el número que decide si vale la pena construir la ejecución.
      const seen = (s.observations || {}).SH_WINDOWS || 0;
      const good = (s.observations || {}).SH_QUOTABLE || 0;
      const pct = seen ? ((good / seen) * 100).toFixed(1) : null;
      html +=
        `<div class="ss-mart-row mt-2" style="border-top:1px solid var(--ss-border)">
           <span><strong>Observación (no opera)</strong></span>
           <span class="ss-mart-val">${pct === null ? "—" : pct + "%"}</span>
         </div>` +
        obs.map(([key, count]) => `
          <div class="ss-mart-row">
            <span>${esc(OBS_LABELS[key] || key)}</span>
            <span class="ss-mart-val">${count}</span>
          </div>`).join("");
    }

    el.innerHTML = html;
  }

  function renderLog(s) {
    const el = $("log");
    if (!el) return;
    el.innerHTML = (s.log || []).slice(-40).reverse().map((e) => {
      const lvl = e.level || "info";
      const cls = lvl === "error" ? "lvl-err" : lvl === "warn" ? "lvl-warn"
                : lvl === "success" ? "lvl-ok" : "";
      const icon = lvl === "error" ? "✖" : lvl === "warn" ? "⚠"
                 : lvl === "success" ? "✓" : "·";
      return `<li class="${cls}"><span class="t">${icon}</span> ${esc(e.message)}</li>`;
    }).join("");
  }

  /* Shared with charts.js and trades.js — they load after this file. */
  window.SS = {
    $, COLORS, STRAT_LABELS, STRAT_COLORS,
    fmtMoney, fmtSigned, fmtPct, fmtPrice, fmtSpot, fmtDuration, fmtTime,
    fmtDateTime, esc,
  };

  // ── poll ──
  let timer = null;
  function startPolling() {
    if (timer) return;
    refresh();
    timer = setInterval(refresh, 1000);
  }
  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  /* No point polling a tab nobody is looking at — it kept a request per second
   * running against the bot for every forgotten browser tab. */
  document.addEventListener("visibilitychange", () => {
    document.hidden ? stopPolling() : startPolling();
  });

  startPolling();
})();
