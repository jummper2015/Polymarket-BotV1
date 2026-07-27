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
  const fmtPct = (v) => (v == null || isNaN(v) ? "—" : (v * 100).toFixed(1) + "%");
  const fmtPrice = (v) => (v == null ? "—" : Number(v).toFixed(4));
  const fmtSpot  = (v) => (v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 }));
  const fmtTime  = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  };
  const fmtDate = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return `${String(d.getMonth()+1).padStart(2,"0")}/${String(d.getDate()).padStart(2,"0")} ${fmtTime(epoch)}`;
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
    ee_traded: "EE Operado", ee_hedged: "EE Hedgeado", ee_tp: "EE Take-Profit",
  };
  const STATUS_CLASS = {
    watching: "ok", traded: "ok", hedged: "ok", mm_placed: "ok",
    ee_traded: "ok", ee_hedged: "ok", ee_tp: "ok",
    holding: "warn", loading_market: "warn", sold: "warn",
    error: "err",
  };

  const MARKET_ICONS = { btc: "₿", sol: "◎", eth: "Ξ", btc15: "🌙" };
  const MARKET_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea", btc15: "#a78bfa" };

  const STRAT_LABELS = {
    trigger:     "⚡ Trigger",
    mm:          "📦 Box Builder",
    early_entry: "🎯 Early Entry",
    corridor:    "🌙 Corridor",
  };
  const STRAT_CLASS  = {
    trigger: "strat-trigger", mm: "strat-mm",
    early_entry: "strat-ee",  corridor: "strat-corridor",
  };
  const STRAT_COLOR  = {
    trigger: "#f1b44c", mm: "#5a8dee",
    early_entry: "#a78bfa", corridor: "#a78bfa",
  };

  // ── element refs ─────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);

  // ── chart state ──────────────────────────────────────────────────────────
  let _chartTab = "cumulative";
  let _lastChartData = null;

  // ── toggle market ────────────────────────────────────────────────────────
  async function toggleMarket(sym) {
    try {
      const resp = await fetch(`toggle-market/${sym}`, { method: "POST", cache: "no-store" });
      if (!resp.ok) return;
      await resp.json();
    } catch (_) {}
  }

  // ── render header ────────────────────────────────────────────────────────
  function renderHeader(s) {
    const c = s.config || {};
    const mb = $("mode-badge");
    if (mb) {
      mb.textContent = (c.mode || "paper").toUpperCase();
      mb.className = "badge " + (c.mode === "real" ? "real" : "paper");
    }

    const markets = s.markets || {};

    // 5m market WS badges
    ["btc", "sol", "eth"].forEach((sym) => {
      const el = $("ws-" + sym);
      if (!el) return;
      const m = markets[sym];
      const ok = m && m.ws_connected;
      el.className = "badge " + (ok ? "ok" : "err");
    });

    // BTC 15m Corridor WS badge (only show when corridor enabled)
    const ws15El = $("ws-btc15");
    if (ws15El) {
      const m15 = markets["btc15"];
      const ccEnabled = m15 && m15.config && m15.config.cc_enabled;
      ws15El.style.display = ccEnabled ? "" : "none";
      if (ccEnabled) {
        ws15El.className = "badge " + (m15.ws_connected ? "ok" : "err");
      }
    }

    const sp = $("spot-prices");
    if (sp) {
      // Show BTC, SOL, ETH spot prices (btc15 shares BTC price — skip it)
      sp.innerHTML = ["btc", "sol", "eth"].map((sym) => {
        const m = markets[sym];
        if (!m) return "";
        return `<div class="spot-item">
          <span class="spot-label" style="color:${MARKET_COLOR[sym]}">${sym.toUpperCase()}</span>
          <span class="spot-val">${fmtSpot(m.spot_price)}</span>
        </div>`;
      }).join("");
    }
  }

  // ── render global KPIs ───────────────────────────────────────────────────
  function renderKpis(s) {
    const st = s.combined_stats || {};
    const resolved = (st.wins || 0) + (st.losses || 0);

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
    if (roi) roi.textContent = "ROI " + (resolved > 0 ? fmtPct(st.roi) : "—");

    // ── Win rate: show — when no resolved trades ──────────────────────────
    const wr = $("kpi-winrate");
    if (wr) {
      wr.textContent = resolved > 0 ? fmtPct(st.win_rate) : "—";
      wr.className = "kpi-value" + (
        resolved === 0 ? "" :
        st.win_rate >= 0.5 ? " up" : " down"
      );
    }
    const wl = $("kpi-wl");
    if (wl) {
      wl.textContent = resolved > 0
        ? `${st.wins || 0}V / ${st.losses || 0}D (${resolved} resueltas)`
        : "Sin operaciones resueltas";
    }

    const tr = $("kpi-trades");
    if (tr) tr.textContent = st.trades || 0;
    const op = $("kpi-open");
    if (op) op.textContent = `${st.open || 0} abiertas`;

    const up = $("kpi-uptime");
    if (up) up.textContent = fmtDuration(st.uptime_seconds);
    const upt = $("uptime");
    if (upt) upt.textContent = fmtDuration(st.uptime_seconds);
  }

  // ── render strategy metrics ──────────────────────────────────────────────
  function renderStrategyMetrics(s) {
    const el = $("strat-metrics");
    if (!el) return;
    const ss = s.combined_strategy_stats || {};
    const markets = s.markets || {};
    const btcCfg  = (markets.btc && markets.btc.config) || s.config || {};
    const btc15Cfg = (markets.btc15 && markets.btc15.config) || {};

    const activeStrat  = btcCfg.active_strategy || "trigger";
    const earlyEnabled = btcCfg.early_entry_enabled || false;
    const ccEnabled    = btc15Cfg.cc_enabled || false;

    const stratOrder = ["trigger", "mm", "early_entry", "corridor"];
    el.innerHTML = stratOrder.map((key) => {
      const st = ss[key] || { trades: 0, wins: 0, losses: 0, win_rate: 0, pnl: 0, roi: 0 };
      const resolved = (st.wins || 0) + (st.losses || 0);
      const isActive =
        (key === "trigger"     && (activeStrat === "trigger" || activeStrat === "both")) ||
        (key === "mm"          && (activeStrat === "market_making" || activeStrat === "both")) ||
        (key === "early_entry" && earlyEnabled) ||
        (key === "corridor"    && ccEnabled);
      const activeDot = isActive
        ? `<span class="strat-active-dot"></span>`
        : `<span class="strat-inactive-dot"></span>`;
      const pnlCls = st.pnl >= 0 ? "pnl-pos" : "pnl-neg";
      const wrDisplay = resolved > 0 ? fmtPct(st.win_rate) : "—";
      return `
        <div class="strat-metric-card">
          <div class="strat-metric-header">
            <span class="tag ${STRAT_CLASS[key] || 'strat-trigger'}">${STRAT_LABELS[key]}</span>
            ${activeDot}
          </div>
          <div class="strat-metric-row">
            <span class="strat-metric-label">Trades</span>
            <span class="strat-metric-val">${st.trades}</span>
          </div>
          <div class="strat-metric-row">
            <span class="strat-metric-label">V / D</span>
            <span class="strat-metric-val">${st.wins}V / ${st.losses}D</span>
          </div>
          <div class="strat-metric-row">
            <span class="strat-metric-label">Win Rate</span>
            <span class="strat-metric-val ${resolved > 0 ? (st.win_rate >= 0.5 ? 'pnl-pos' : 'pnl-neg') : ''}">${wrDisplay}</span>
          </div>
          <div class="strat-metric-row">
            <span class="strat-metric-label">P&L</span>
            <span class="strat-metric-val ${pnlCls}">${fmtSigned(st.pnl)}</span>
          </div>
          <div class="strat-metric-row">
            <span class="strat-metric-label">ROI</span>
            <span class="strat-metric-val ${pnlCls}">${resolved > 0 ? fmtPct(st.roi) : '—'}</span>
          </div>
        </div>`;
    }).join("");
  }

  // ── trades chart ─────────────────────────────────────────────────────────

  function collectChartTrades(s) {
    const markets = s.markets || {};
    let all = [];
    for (const [sym, m] of Object.entries(markets)) {
      (m.trades || []).forEach((t) => {
        if (t.pnl != null) all.push({ ...t, _sym: sym });
      });
    }
    all.sort((a, b) => (a.opened_at || 0) - (b.opened_at || 0));
    return all;
  }

  function renderTradesChart(s) {
    _lastChartData = s;
    const emptyEl  = $("chart-empty");
    const svgEl    = $("trades-chart");
    const legendEl = $("chart-legend");
    if (!svgEl || !emptyEl) return;

    const trades = collectChartTrades(s);

    // Legend (always visible)
    if (legendEl) {
      legendEl.innerHTML = Object.entries(STRAT_LABELS).map(([key, label]) => `
        <div class="chart-legend-item">
          <div class="chart-legend-dot" style="background:${STRAT_COLOR[key]}"></div>
          <span>${label}</span>
        </div>`).join("") +
        `<div class="chart-legend-item" style="margin-left:auto;">
          <div class="chart-legend-dot" style="background:#1cc88a;border:2px solid #fff;"></div><span>Ganada</span>
        </div>
        <div class="chart-legend-item">
          <div class="chart-legend-dot" style="background:#e74a3b;border:2px solid #fff;"></div><span>Perdida</span>
        </div>`;
    }

    if (trades.length === 0) {
      emptyEl.style.display = "flex";
      svgEl.style.display   = "none";
      return;
    }
    emptyEl.style.display = "none";
    svgEl.style.display   = "block";

    if (_chartTab === "cumulative") {
      drawCumulativeChart(svgEl, trades);
    } else {
      drawPerTradeChart(svgEl, trades);
    }
  }

  function drawCumulativeChart(svgEl, trades) {
    const W   = svgEl.parentElement.clientWidth || 700;
    const H   = 230;
    const PAD = { top: 24, right: 24, bottom: 36, left: 72 };
    const IW  = W - PAD.left - PAD.right;
    const IH  = H - PAD.top - PAD.bottom;

    svgEl.setAttribute("height", H);
    svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);

    // Build cumulative series with one extra zero-point at the start
    let cum = 0;
    const pts = [{ x: 0, y: 0, t: null }];
    trades.forEach((t, i) => { cum += t.pnl || 0; pts.push({ x: i + 1, y: cum, t }); });

    const n    = pts.length;
    const minY = Math.min(0, ...pts.map((p) => p.y));
    const maxY = Math.max(0, ...pts.map((p) => p.y));
    const ranY = maxY - minY || 1;

    const sx = (i) => PAD.left + (i / (n - 1)) * IW;
    const sy = (v) => PAD.top  + (1 - (v - minY) / ranY) * IH;
    const y0 = sy(0);

    // Y grid lines + labels (5 ticks)
    const yTicks = 5;
    let svgStr = `<defs>
      <linearGradient id="cg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#5a8dee" stop-opacity="0.22"/>
        <stop offset="100%" stop-color="#5a8dee" stop-opacity="0.02"/>
      </linearGradient>
    </defs>`;

    for (let i = 0; i <= yTicks; i++) {
      const v  = minY + (i / yTicks) * ranY;
      const cy = sy(v);
      const lbl = (v >= 0 ? "+" : "") + "$" + Math.abs(v).toFixed(2);
      svgStr += `<line x1="${PAD.left}" y1="${cy}" x2="${W - PAD.right}" y2="${cy}"
        stroke="rgba(255,255,255,${Math.abs(v) < 0.001 ? '0.20' : '0.06'})"
        stroke-dasharray="${Math.abs(v) < 0.001 ? '1,0' : '4,4'}"/>`;
      svgStr += `<text x="${PAD.left - 8}" y="${cy + 4}" text-anchor="end"
        font-size="11" fill="rgba(255,255,255,${Math.abs(v) < 0.001 ? '0.5' : '0.35'})"
        font-family="ui-monospace,monospace">${lbl}</text>`;
    }

    // Area fill
    const linePts = pts.map((p, i) => `${sx(i)},${sy(p.y)}`).join(" ");
    const closePts = `${sx(n - 1)},${y0} ${sx(0)},${y0}`;
    svgStr += `<polygon points="${linePts} ${closePts}" fill="url(#cg)"/>`;

    // Main line
    svgStr += `<polyline points="${linePts}"
      fill="none" stroke="#5a8dee" stroke-width="2.5"
      stroke-linejoin="round" stroke-linecap="round"/>`;

    // Zero line label
    svgStr += `<text x="${W - PAD.right + 4}" y="${y0 + 4}"
      font-size="10" fill="rgba(255,255,255,0.4)">$0</text>`;

    // Trade markers (skip the synthetic zero-point at index 0)
    pts.slice(1).forEach((p, rawIdx) => {
      const i     = rawIdx + 1;
      const cx    = sx(i);
      const cy    = sy(p.y);
      const color = STRAT_COLOR[p.t.strategy] || "#5a8dee";
      const won   = p.t.status === "won";
      svgStr += `<circle cx="${cx}" cy="${cy}" r="5.5"
        fill="${color}" fill-opacity="0.85"
        stroke="${won ? '#1cc88a' : '#e74a3b'}" stroke-width="2"
        class="chart-dot" data-idx="${rawIdx}"
        style="cursor:pointer"/>`;
    });

    // X-axis: show first, last, and a few intermediate labels (trade timestamps)
    const xStep = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += xStep) {
      const p = pts[i];
      if (!p.t) continue;
      const cx  = sx(i);
      const lbl = fmtTime(p.t.opened_at);
      svgStr += `<text x="${cx}" y="${H - 6}" text-anchor="middle"
        font-size="10" fill="rgba(255,255,255,0.3)">${lbl}</text>`;
    }

    // Final P&L callout
    const lastPt = pts[pts.length - 1];
    const totalPnl = lastPt.y;
    const calloutCls = totalPnl >= 0 ? "#1cc88a" : "#e74a3b";
    svgStr += `<text x="${W - PAD.right}" y="${PAD.top - 8}"
      text-anchor="end" font-size="12" font-weight="600" fill="${calloutCls}">
      P&amp;L ${fmtSigned(totalPnl)}
    </text>`;

    svgEl.innerHTML = svgStr;

    // Tooltip on hover
    const tooltip = $("chart-tooltip");
    svgEl.querySelectorAll(".chart-dot").forEach((dot) => {
      const idx = parseInt(dot.dataset.idx, 10);
      const trade = trades[idx];
      if (!trade || !tooltip) return;
      dot.addEventListener("mouseenter", (e) => {
        const prevPnl = idx > 0 ? trades.slice(0, idx).reduce((s, t) => s + (t.pnl || 0), 0) : 0;
        tooltip.innerHTML = `
          <div class="chart-tooltip-title" style="color:${STRAT_COLOR[trade.strategy] || '#fff'}">
            ${STRAT_LABELS[trade.strategy] || trade.strategy}
          </div>
          <div class="chart-tooltip-row"><span>Mercado</span><span class="chart-tooltip-val" style="color:${MARKET_COLOR[trade._sym] || '#aaa'}">${trade._sym.toUpperCase()}</span></div>
          <div class="chart-tooltip-row"><span>Lado</span><span class="chart-tooltip-val">${trade.side}</span></div>
          <div class="chart-tooltip-row"><span>Precio</span><span class="chart-tooltip-val">${Number(trade.price).toFixed(4)}</span></div>
          <div class="chart-tooltip-row"><span>Estado</span><span class="chart-tooltip-val" style="color:${trade.status === 'won' ? '#1cc88a' : '#e74a3b'}">${trade.status}</span></div>
          <div class="chart-tooltip-row"><span>P&amp;L trade</span><span class="chart-tooltip-val" style="color:${(trade.pnl || 0) >= 0 ? '#1cc88a' : '#e74a3b'}">${fmtSigned(trade.pnl)}</span></div>
          <div class="chart-tooltip-row"><span>P&amp;L acum.</span><span class="chart-tooltip-val" style="color:${(prevPnl + (trade.pnl || 0)) >= 0 ? '#1cc88a' : '#e74a3b'}">${fmtSigned(prevPnl + (trade.pnl || 0))}</span></div>
          <div class="chart-tooltip-row" style="margin-top:4px;color:rgba(255,255,255,0.4);font-size:11px;"><span>${fmtDate(trade.opened_at)}</span></div>`;
        positionTooltip(tooltip, e);
        tooltip.style.display = "block";
      });
      dot.addEventListener("mousemove", (e) => positionTooltip(tooltip, e));
      dot.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
    });
  }

  function drawPerTradeChart(svgEl, trades) {
    const W   = svgEl.parentElement.clientWidth || 700;
    const H   = 230;
    const PAD = { top: 24, right: 24, bottom: 36, left: 72 };
    const IW  = W - PAD.left - PAD.right;
    const IH  = H - PAD.top - PAD.bottom;

    svgEl.setAttribute("height", H);
    svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);

    const n    = trades.length;
    const pnls = trades.map((t) => t.pnl || 0);
    const maxAbs = Math.max(0.01, ...pnls.map(Math.abs));

    const sx = (i) => PAD.left + ((i + 0.5) / n) * IW;
    const barH = (v) => Math.max(2, Math.abs(v) / maxAbs * (IH / 2 - 6));
    const midY = PAD.top + IH / 2;

    let svgStr = "";

    // Y grid
    [maxAbs / 2, 0, -maxAbs / 2].forEach((v, gi) => {
      const cy  = midY - (v / maxAbs) * (IH / 2);
      const lbl = (v >= 0 ? "+" : "") + "$" + Math.abs(v).toFixed(2);
      svgStr += `<line x1="${PAD.left}" y1="${cy}" x2="${W - PAD.right}" y2="${cy}"
        stroke="rgba(255,255,255,${gi === 1 ? '0.20' : '0.06'})"
        stroke-dasharray="${gi === 1 ? '1,0' : '4,4'}"/>`;
      svgStr += `<text x="${PAD.left - 8}" y="${cy + 4}" text-anchor="end"
        font-size="11" fill="rgba(255,255,255,0.35)" font-family="ui-monospace,monospace">${lbl}</text>`;
    });

    const barW = Math.max(4, Math.min(24, IW / n - 3));

    trades.forEach((t, i) => {
      const cx    = sx(i);
      const pnl   = t.pnl || 0;
      const bH    = barH(pnl);
      const bY    = pnl >= 0 ? midY - bH : midY;
      const color = STRAT_COLOR[t.strategy] || "#5a8dee";
      const won   = t.status === "won";
      svgStr += `<rect x="${cx - barW / 2}" y="${bY}" width="${barW}" height="${bH}"
        fill="${color}" fill-opacity="0.75" rx="3"
        stroke="${won ? '#1cc88a' : '#e74a3b'}" stroke-width="1.5"
        class="chart-bar" data-idx="${i}" style="cursor:pointer"/>`;
    });

    svgEl.innerHTML = svgStr;

    const tooltip = $("chart-tooltip");
    svgEl.querySelectorAll(".chart-bar").forEach((bar) => {
      const idx = parseInt(bar.dataset.idx, 10);
      const trade = trades[idx];
      if (!trade || !tooltip) return;
      bar.addEventListener("mouseenter", (e) => {
        tooltip.innerHTML = `
          <div class="chart-tooltip-title" style="color:${STRAT_COLOR[trade.strategy] || '#fff'}">
            ${STRAT_LABELS[trade.strategy] || trade.strategy}
          </div>
          <div class="chart-tooltip-row"><span>Mercado</span><span class="chart-tooltip-val" style="color:${MARKET_COLOR[trade._sym] || '#aaa'}">${trade._sym.toUpperCase()}</span></div>
          <div class="chart-tooltip-row"><span>Lado</span><span class="chart-tooltip-val">${trade.side}</span></div>
          <div class="chart-tooltip-row"><span>Precio</span><span class="chart-tooltip-val">${Number(trade.price).toFixed(4)}</span></div>
          <div class="chart-tooltip-row"><span>Shares</span><span class="chart-tooltip-val">${Number(trade.shares).toFixed(2)}</span></div>
          <div class="chart-tooltip-row"><span>Estado</span><span class="chart-tooltip-val" style="color:${trade.status === 'won' ? '#1cc88a' : '#e74a3b'}">${trade.status}</span></div>
          <div class="chart-tooltip-row"><span>P&amp;L</span><span class="chart-tooltip-val" style="color:${(trade.pnl || 0) >= 0 ? '#1cc88a' : '#e74a3b'}">${fmtSigned(trade.pnl)}</span></div>
          <div class="chart-tooltip-row" style="margin-top:4px;color:rgba(255,255,255,0.4);font-size:11px;"><span>${fmtDate(trade.opened_at)}</span></div>`;
        positionTooltip(tooltip, e);
        tooltip.style.display = "block";
      });
      bar.addEventListener("mousemove", (e) => positionTooltip(tooltip, e));
      bar.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
    });
  }

  function positionTooltip(tooltip, e) {
    const x = e.clientX;
    const y = e.clientY;
    const tw = tooltip.offsetWidth  || 200;
    const th = tooltip.offsetHeight || 100;
    const left = x + tw + 16 > window.innerWidth ? x - tw - 12 : x + 14;
    const top  = y + th + 16 > window.innerHeight ? y - th - 8 : y + 8;
    tooltip.style.left = left + "px";
    tooltip.style.top  = top  + "px";
  }

  function bindChartTabs() {
    document.querySelectorAll(".chart-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".chart-tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        _chartTab = btn.dataset.tab;
        if (_lastChartData) renderTradesChart(_lastChartData);
      });
    });
  }

  // ── render one market panel ──────────────────────────────────────────────
  function renderMarket(sym, m) {
    const panel = $("panel-" + sym);
    if (!panel) return;

    const icon    = MARKET_ICONS[sym] || sym.toUpperCase();
    const color   = MARKET_COLOR[sym] || "#888";
    const stCls   = STATUS_CLASS[m.bot_status] || "";
    const stLbl   = STATUS_LABEL[m.bot_status] || m.bot_status;
    const wsCls   = m.ws_connected ? "ok" : "err";
    const slug    = m.current_slug || "—";
    const ttl     = m.seconds_remaining != null ? fmtDuration(m.seconds_remaining) : "—";
    const up      = m.last_up_price;
    const down    = m.last_down_price;
    const st      = m.stats || {};
    const pnlCls  = st.resolved_pnl >= 0 ? "pnl-pos" : "pnl-neg";
    const enabled = m.market_enabled !== false;
    const resolved = (st.wins || 0) + (st.losses || 0);

    // Special corridor panel for btc15
    if (sym === "btc15") {
      const ccEnabled = m.config && m.config.cc_enabled;
      const ccPaused  = m.config && m.config.cc_paused;
      panel.innerHTML = `
        <div class="market-header">
          <div class="market-title">
            <span class="market-icon" style="color:${color}">${icon}</span>
            <div>
              <div class="market-name">Bitcoin 15m <span class="muted" style="font-size:12px;font-weight:400;">(Corridor)</span>
                ${ccPaused ? `<span class="badge err" style="font-size:10px;padding:2px 6px;margin-left:6px;">KILL SWITCH</span>` : ""}
              </div>
              <div class="mono muted" style="font-size:11px;">${slug}</div>
            </div>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <span class="badge ${stCls}">${stLbl}</span>
            <span class="badge ${wsCls}">WS ●</span>
          </div>
        </div>

        <div class="prices" style="margin-top:14px;">
          <div class="price up">
            <span class="side">▲ SUBE 15m</span>
            <span class="value">${up != null ? up.toFixed(4) : "—"}</span>
            <div class="bar"><div class="bar-fill" style="width:${up != null ? (up * 100).toFixed(1) : 0}%"></div></div>
          </div>
          <div class="price down">
            <span class="side">▼ BAJA 15m</span>
            <span class="value">${down != null ? down.toFixed(4) : "—"}</span>
            <div class="bar"><div class="bar-fill" style="width:${down != null ? (down * 100).toFixed(1) : 0}%"></div></div>
          </div>
        </div>

        <div class="market-meta">
          <span>⏱ <strong>${ttl}</strong></span>
          <span>Pares <strong>${st.trades || 0}</strong></span>
          <span>Victoria <strong class="${resolved > 0 && st.win_rate >= 0.5 ? 'pnl-pos' : resolved > 0 ? 'pnl-neg' : ''}">${resolved > 0 ? (st.win_rate * 100).toFixed(0) + "%" : "—"}</strong></span>
          <span class="${pnlCls}">P&L <strong>${fmtSigned(st.resolved_pnl || 0)}</strong></span>
        </div>
      `;
      return;
    }

    // Standard 5m panel
    const toggleLabel = enabled ? "⏸ Desactivar" : "▶ Activar";
    const toggleCls   = enabled ? "btn-mkt-toggle active" : "btn-mkt-toggle inactive";
    const wrDisplay   = resolved > 0 ? `${(st.win_rate * 100).toFixed(0)}%` : "—";

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
          <button class="${toggleCls}" data-sym="${sym}">${toggleLabel}</button>
        </div>
      </div>

      ${!enabled ? `<div class="market-disabled-overlay">Mercado Desactivado</div>` : `
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
        <span>Victoria <strong class="${resolved > 0 && st.win_rate >= 0.5 ? 'pnl-pos' : resolved > 0 ? 'pnl-neg' : ''}">${wrDisplay}</strong></span>
        <span class="${pnlCls}">P&L <strong>${fmtSigned(st.resolved_pnl || 0)}</strong></span>
      </div>`}
    `;

    const btn = panel.querySelector(".btn-mkt-toggle");
    if (btn) btn.addEventListener("click", () => toggleMarket(sym));
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

    const MKT_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea", btc15: "#a78bfa" };
    tbody.innerHTML = allTrades.map((t) => {
      const pnlCls  = t.pnl == null ? "" : t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
      const pnlText = t.pnl == null ? "—" : fmtSigned(t.pnl);
      const typeTag = t.is_hedge
        ? `<span class="tag hedge">HEDGE</span>`
        : `<span class="tag initial">INIT</span>`;
      let stratTag;
      if      (t.strategy === "mm")          stratTag = `<span class="tag strat-mm">BOX</span>`;
      else if (t.strategy === "early_entry") stratTag = `<span class="tag strat-ee">EE</span>`;
      else if (t.strategy === "corridor")    stratTag = `<span class="tag strat-corridor">CORR</span>`;
      else                                   stratTag = `<span class="tag strat-trigger">TRG</span>`;
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
    const MKT_COLOR = { btc: "#f7931a", sol: "#9945ff", eth: "#627eea", btc15: "#a78bfa" };
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
    logEl.querySelectorAll("li").forEach((li, i) => {
      const msgEl = li.querySelector(".msg");
      if (msgEl && items[i]) msgEl.textContent = items[i].message;
    });
  }

  // ── show/hide corridor section ───────────────────────────────────────────
  function updateCorridorSection(s) {
    const section = $("corridor-section");
    if (!section) return;
    const markets = s.markets || {};
    const btc15   = markets.btc15;
    const ccEnabled = btc15 && btc15.config && btc15.config.cc_enabled;
    section.style.display = ccEnabled ? "" : "none";
  }

  // ── main render ──────────────────────────────────────────────────────────
  function render(s) {
    renderHeader(s);
    renderKpis(s);
    renderStrategyMetrics(s);
    renderTradesChart(s);
    updateCorridorSection(s);
    const markets = s.markets || {};
    // Render 5m markets
    ["btc", "sol", "eth"].forEach((sym) => {
      if (markets[sym]) renderMarket(sym, markets[sym]);
    });
    // Render corridor panel (inside the corridor section — only if visible)
    if (markets.btc15) renderMarket("btc15", markets.btc15);
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

  bindChartTabs();
  poll();
  setInterval(poll, 1000);

  // Redraw chart on window resize (SVG is responsive but needs recompute)
  let _resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => { if (_lastChartData) renderTradesChart(_lastChartData); }, 200);
  });
})();
