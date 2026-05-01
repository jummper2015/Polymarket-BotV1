(function () {
  // ── formatters ──────────────────────────────────────────────────────────
  const fmtMoney = (v) => {
    if (v == null || Number.isNaN(v)) return "$0.00";
    const sign = v < 0 ? "-" : "";
    return sign + "$" + Math.abs(v).toFixed(2);
  };
  const fmtSigned = (v) => {
    if (v == null || Number.isNaN(v)) return "$0.00";
    return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
  };
  const fmtPct = (v) =>
    v == null || Number.isNaN(v) ? "0.00%" : (v * 100).toFixed(2) + "%";
  const fmtPrice = (v) => (v == null ? "—" : Number(v).toFixed(4));
  const fmtBtc = (v) =>
    v == null ? "—" : "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  const fmtTime = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0"))
      .join(":");
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

  // ── element refs ─────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const els = {
    modeBadge: $("mode-badge"),
    statusBadge: $("status-badge"),
    wsBadge: $("ws-badge"),
    btcPrice: $("btc-price"),
    btcAge: $("btc-age"),
    kpiBankroll: $("kpi-bankroll"),
    kpiBankrollSub: $("kpi-bankroll-sub"),
    kpiPnl: $("kpi-pnl"),
    kpiRoi: $("kpi-roi"),
    kpiWinrate: $("kpi-winrate"),
    kpiWl: $("kpi-wl"),
    kpiTrades: $("kpi-trades"),
    kpiOpen: $("kpi-open"),
    kpiWindows: $("kpi-windows"),
    kpiTraded: $("kpi-traded"),
    windowSlug: $("window-slug"),
    priceUp: $("price-up"),
    priceDown: $("price-down"),
    barUp: $("bar-up"),
    barDown: $("bar-down"),
    metaTrigger: $("meta-trigger"),
    metaBuy: $("meta-buy"),
    metaHedge: $("meta-hedge"),
    metaLastmin: $("meta-lastmin"),
    metaTtl: $("meta-ttl"),
    chart: $("price-chart"),
    log: $("log"),
    trades: $("trades-body"),
    uptime: $("uptime"),
    cfgTrigger: $("cfg-trigger"),
    cfgBuy: $("cfg-buy"),
    cfgMaxTrades: $("cfg-max-trades"),
    cfgHedge: $("cfg-hedge"),
    cfgLastmin: $("cfg-lastmin"),
    cfgSaveMsg: $("cfg-save-msg"),
    btnPaper: $("btn-paper"),
    btnReal: $("btn-real"),
    readinessBadge: $("readiness-badge"),
    readinessList: $("readiness-steps"),
  };

  // ── config form state ────────────────────────────────────────────────────
  let _selectedMode = "paper";

  els.btnPaper.addEventListener("click", () => setModeUI("paper"));
  els.btnReal.addEventListener("click", () => setModeUI("real"));

  function setModeUI(mode) {
    _selectedMode = mode;
    els.btnPaper.classList.toggle("active", mode === "paper");
    els.btnReal.classList.toggle("active", mode === "real");
  }

  $("cfg-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      trigger_price: parseFloat(els.cfgTrigger.value),
      buy_amount: parseFloat(els.cfgBuy.value),
      max_trades_per_window: parseInt(els.cfgMaxTrades.value, 10),
      hedge_threshold: parseFloat(els.cfgHedge.value),
      last_minute_seconds: parseInt(els.cfgLastmin.value, 10),
      mode: _selectedMode,
    };
    // Remove NaN values
    Object.keys(body).forEach((k) => {
      if (typeof body[k] === "number" && isNaN(body[k])) delete body[k];
    });
    try {
      const resp = await fetch("config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        const keys = Object.keys(data.updated || {});
        showMsg("✓ Guardado: " + (keys.length ? keys.join(", ") : "sin cambios"), "ok");
      } else {
        showMsg("✗ " + (data.error || "Error al guardar"), "err");
      }
    } catch (err) {
      showMsg("✗ Error de red: " + err.message, "err");
    }
  });

  function showMsg(text, type) {
    els.cfgSaveMsg.textContent = text;
    els.cfgSaveMsg.className = "cfg-msg " + type;
    setTimeout(() => { els.cfgSaveMsg.textContent = ""; els.cfgSaveMsg.className = "cfg-msg"; }, 4000);
  }

  // ── renderers ────────────────────────────────────────────────────────────
  function renderBadges(s) {
    els.modeBadge.textContent = `modo: ${s.mode}`;
    els.modeBadge.className = "badge " + (s.mode === "real" ? "real" : "paper");

    let cls = "badge";
    const label = `estado: ${s.bot_status}`;
    if (["watching", "traded", "hedged"].includes(s.bot_status)) cls += " ok";
    else if (s.bot_status === "error") cls += " err";
    else if (["loading_market", "holding"].includes(s.bot_status)) cls += " warn";
    els.statusBadge.textContent = s.bot_message ? `${s.bot_status} — ${s.bot_message}` : label;
    els.statusBadge.className = cls;

    els.wsBadge.textContent = s.ws_connected ? "ws: conectado" : "ws: offline";
    els.wsBadge.className = "badge " + (s.ws_connected ? "ok" : "warn");
  }

  function renderBtcPrice(s) {
    els.btcPrice.textContent = fmtBtc(s.btc_price);
    if (s.btc_price_updated_at) {
      const ageS = Math.floor(Date.now() / 1000 - s.btc_price_updated_at);
      els.btcAge.textContent = ageS < 60 ? `${ageS}s ago` : `${Math.floor(ageS / 60)}m ago`;
    } else {
      els.btcAge.textContent = "";
    }
  }

  function renderKpis(s) {
    const st = s.stats;
    els.kpiBankroll.textContent = fmtMoney(st.bankroll);
    els.kpiBankroll.classList.toggle("up", st.bankroll > st.starting_bankroll);
    els.kpiBankroll.classList.toggle("down", st.bankroll < st.starting_bankroll);
    els.kpiBankrollSub.textContent = `inicio ${fmtMoney(st.starting_bankroll)} · efectivo ${fmtMoney(st.available_cash)}`;
    els.kpiPnl.textContent = fmtSigned(st.resolved_pnl);
    els.kpiPnl.classList.toggle("up", st.resolved_pnl > 0);
    els.kpiPnl.classList.toggle("down", st.resolved_pnl < 0);
    els.kpiRoi.textContent = `ROI ${fmtPct(st.roi)}`;
    els.kpiWinrate.textContent = fmtPct(st.win_rate);
    els.kpiWl.textContent = `${st.wins}W / ${st.losses}L`;
    els.kpiTrades.textContent = st.trades;
    els.kpiOpen.textContent = `${st.open} abiertas · invertido ${fmtMoney(st.total_invested)}`;
    els.kpiWindows.textContent = st.windows_observed;
    els.kpiTraded.textContent = `${st.windows_traded} operadas`;
    els.uptime.textContent = fmtDuration(st.uptime_seconds);
  }

  let _cfgPopulated = false;
  function renderConfig(s) {
    // Only populate form fields once (don't override while user is editing)
    if (!_cfgPopulated) {
      els.cfgTrigger.value = s.trigger_price;
      els.cfgBuy.value = s.buy_amount;
      els.cfgMaxTrades.value = s.max_trades_per_window;
      els.cfgHedge.value = s.hedge_threshold;
      els.cfgLastmin.value = s.last_minute_seconds;
      setModeUI(s.mode);
      _cfgPopulated = true;
    }
    // Keep mode toggle in sync with server state
    els.btnPaper.classList.toggle("active", s.mode === "paper");
    els.btnReal.classList.toggle("active", s.mode === "real");
    if (_selectedMode !== s.mode && document.activeElement !== els.btnPaper && document.activeElement !== els.btnReal) {
      _selectedMode = s.mode;
    }
  }

  function renderWindow(s) {
    els.windowSlug.textContent = s.current_slug || "—";
    els.priceUp.textContent = fmtPrice(s.last_up_price);
    els.priceDown.textContent = fmtPrice(s.last_down_price);
    els.barUp.style.width = ((s.last_up_price || 0) * 100).toFixed(2) + "%";
    els.barDown.style.width = ((s.last_down_price || 0) * 100).toFixed(2) + "%";
    els.metaTrigger.textContent = Number(s.trigger_price).toFixed(2);
    els.metaBuy.textContent = "$" + Number(s.buy_amount).toFixed(2);
    els.metaHedge.textContent = Number(s.hedge_threshold || 0.96).toFixed(2);
    els.metaLastmin.textContent = (s.last_minute_seconds || 60) + "s";
    els.metaTtl.textContent =
      s.seconds_remaining != null ? fmtDuration(s.seconds_remaining) : "—";
  }

  function renderReadiness(s) {
    const r = s.real_mode_readiness;
    if (!r) return;
    els.readinessBadge.textContent = r.ready ? "✓ Listo para Real" : "✗ Pendiente";
    els.readinessBadge.className = "badge " + (r.ready ? "ok" : "warn");

    els.readinessList.innerHTML = r.steps
      .map(
        (step) => `
      <li class="readiness-step ${step.done ? "done" : "todo"}">
        <span class="step-icon">${step.done ? "✓" : "○"}</span>
        <div>
          <strong>${step.text}</strong>
          ${step.detail ? `<p class="step-detail muted">${step.detail}</p>` : ""}
        </div>
      </li>`
      )
      .join("");
  }

  function renderLog(s) {
    if (!s.log) return;
    els.log.innerHTML = "";
    const items = [...s.log].reverse();
    for (const entry of items) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="ts">${fmtTime(entry.t)}</span><span class="level ${entry.level}">${entry.level}</span><span class="msg"></span>`;
      li.querySelector(".msg").textContent = entry.message;
      els.log.appendChild(li);
    }
  }

  function renderTrades(s) {
    if (!s.trades || s.trades.length === 0) {
      els.trades.innerHTML = `<tr><td colspan="10" class="empty">Sin trades — esperando trigger.</td></tr>`;
      return;
    }
    els.trades.innerHTML = s.trades
      .map((t) => {
        const pnlCls = t.pnl == null ? "" : t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
        const pnlText = t.pnl == null ? "—" : fmtSigned(t.pnl);
        const typeTag = t.is_hedge
          ? `<span class="tag hedge">HEDGE</span>`
          : `<span class="tag initial">INIT</span>`;
        return `<tr${t.is_hedge ? ' class="row-hedge"' : ""}>
          <td>#${t.id} ${typeTag}</td>
          <td class="mono">${t.window_slug}</td>
          <td><span class="tag ${t.side.toLowerCase()}">${t.side}</span></td>
          <td>${Number(t.price).toFixed(4)}</td>
          <td>${Number(t.shares).toFixed(2)}</td>
          <td>${fmtMoney(t.cost)}</td>
          <td><span class="tag ${t.status}">${t.status}</span></td>
          <td class="${pnlCls}">${pnlText}</td>
          <td><span class="tag ${t.mode}">${t.mode}</span></td>
          <td class="mono">${t.order_id || "—"}</td>
        </tr>`;
      })
      .join("");
  }

  function renderChart(s) {
    const ctx = els.chart.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const cssW = els.chart.clientWidth || 600;
    const cssH = 160;
    if (els.chart.width !== Math.floor(cssW * dpr) || els.chart.height !== Math.floor(cssH * dpr)) {
      els.chart.width = Math.floor(cssW * dpr);
      els.chart.height = Math.floor(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = (i / 4) * cssH;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cssW, y); ctx.stroke();
    }

    const trigger = s.trigger_price;
    if (trigger && trigger > 0 && trigger < 1) {
      const y = cssH - trigger * cssH;
      ctx.strokeStyle = "rgba(241,180,76,0.7)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cssW, y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(241,180,76,0.9)";
      ctx.font = "11px ui-monospace,monospace";
      ctx.fillText("trigger " + trigger.toFixed(2), 6, y - 4);
    }

    const drawSeries = (points, color) => {
      if (!points || points.length < 2) return;
      const ts = points.map((p) => p.t);
      const minT = ts[0], maxT = ts[ts.length - 1];
      const span = Math.max(1, maxT - minT);
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = ((p.t - minT) / span) * cssW;
        const y = cssH - Math.min(1, Math.max(0, p.p)) * cssH;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };
    const ph = s.price_history || {};
    drawSeries(ph.up, "#1cc88a");
    drawSeries(ph.down, "#e74a3b");
  }

  // ── poll ─────────────────────────────────────────────────────────────────
  async function poll() {
    try {
      const resp = await fetch("state", { cache: "no-store" });
      if (!resp.ok) return;
      const s = await resp.json();
      renderBadges(s);
      renderBtcPrice(s);
      renderKpis(s);
      renderConfig(s);
      renderWindow(s);
      renderReadiness(s);
      renderLog(s);
      renderTrades(s);
      renderChart(s);
    } catch (_) { /* silently retry */ }
  }

  poll();
  setInterval(poll, 1000);
})();
