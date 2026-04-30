(function () {
  const fmtMoney = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "$0.00";
    const sign = v < 0 ? "-" : "";
    return sign + "$" + Math.abs(v).toFixed(2);
  };
  const fmtSignedMoney = (v) => {
    if (v === null || v === undefined || Number.isNaN(v)) return "$0.00";
    const sign = v >= 0 ? "+" : "-";
    return sign + "$" + Math.abs(v).toFixed(2);
  };
  const fmtPct = (v) => (v === null || v === undefined || Number.isNaN(v) ? "0.00%" : (v * 100).toFixed(2) + "%");
  const fmtPrice = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(4));
  const fmtTime = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  };
  const fmtDuration = (sec) => {
    if (sec === null || sec === undefined) return "0s";
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h) return `${h}h ${m}m ${s}s`;
    if (m) return `${m}m ${s}s`;
    return `${s}s`;
  };

  const els = {
    modeBadge: document.getElementById("mode-badge"),
    statusBadge: document.getElementById("status-badge"),
    wsBadge: document.getElementById("ws-badge"),
    kpiBankroll: document.getElementById("kpi-bankroll"),
    kpiBankrollSub: document.getElementById("kpi-bankroll-sub"),
    kpiPnl: document.getElementById("kpi-pnl"),
    kpiRoi: document.getElementById("kpi-roi"),
    kpiWinrate: document.getElementById("kpi-winrate"),
    kpiWl: document.getElementById("kpi-wl"),
    kpiTrades: document.getElementById("kpi-trades"),
    kpiOpen: document.getElementById("kpi-open"),
    kpiWindows: document.getElementById("kpi-windows"),
    kpiTraded: document.getElementById("kpi-traded"),
    windowSlug: document.getElementById("window-slug"),
    priceUp: document.getElementById("price-up"),
    priceDown: document.getElementById("price-down"),
    barUp: document.getElementById("bar-up"),
    barDown: document.getElementById("bar-down"),
    metaTrigger: document.getElementById("meta-trigger"),
    metaBuy: document.getElementById("meta-buy"),
    metaTtl: document.getElementById("meta-ttl"),
    chart: document.getElementById("price-chart"),
    log: document.getElementById("log"),
    trades: document.getElementById("trades-body"),
    uptime: document.getElementById("uptime"),
  };

  function renderBadges(s) {
    els.modeBadge.textContent = `mode: ${s.mode}`;
    els.modeBadge.className = "badge " + (s.mode === "real" ? "real" : "paper");

    let cls = "badge";
    let label = `status: ${s.bot_status}`;
    if (s.bot_status === "watching" || s.bot_status === "traded") cls += " ok";
    else if (s.bot_status === "error") cls += " err";
    else if (s.bot_status === "loading_market") cls += " warn";
    if (s.bot_message) label += ` — ${s.bot_message}`;
    els.statusBadge.textContent = label;
    els.statusBadge.className = cls;

    els.wsBadge.textContent = s.ws_connected ? "ws: connected" : "ws: offline";
    els.wsBadge.className = "badge " + (s.ws_connected ? "ok" : "warn");
  }

  function renderKpis(s) {
    const stats = s.stats;
    els.kpiBankroll.textContent = fmtMoney(stats.bankroll);
    els.kpiBankroll.classList.toggle("up", stats.bankroll > stats.starting_bankroll);
    els.kpiBankroll.classList.toggle("down", stats.bankroll < stats.starting_bankroll);
    els.kpiBankrollSub.textContent = `start ${fmtMoney(stats.starting_bankroll)} · cash ${fmtMoney(stats.available_cash)}`;

    els.kpiPnl.textContent = fmtSignedMoney(stats.resolved_pnl);
    els.kpiPnl.classList.toggle("up", stats.resolved_pnl > 0);
    els.kpiPnl.classList.toggle("down", stats.resolved_pnl < 0);
    els.kpiRoi.textContent = `ROI ${fmtPct(stats.roi)}`;

    els.kpiWinrate.textContent = fmtPct(stats.win_rate);
    els.kpiWl.textContent = `${stats.wins}W / ${stats.losses}L`;

    els.kpiTrades.textContent = stats.trades;
    els.kpiOpen.textContent = `${stats.open} open · invested ${fmtMoney(stats.total_invested)}`;

    els.kpiWindows.textContent = stats.windows_observed;
    els.kpiTraded.textContent = `${stats.windows_traded} traded`;

    els.uptime.textContent = fmtDuration(stats.uptime_seconds);
  }

  function renderWindow(s) {
    els.windowSlug.textContent = s.current_slug || "—";
    els.priceUp.textContent = fmtPrice(s.last_up_price);
    els.priceDown.textContent = fmtPrice(s.last_down_price);
    els.barUp.style.width = ((s.last_up_price || 0) * 100).toFixed(2) + "%";
    els.barDown.style.width = ((s.last_down_price || 0) * 100).toFixed(2) + "%";
    els.metaTrigger.textContent = s.trigger_price.toFixed(2);
    els.metaBuy.textContent = "$" + s.buy_amount.toFixed(2);
    els.metaTtl.textContent = s.seconds_remaining !== null && s.seconds_remaining !== undefined
      ? fmtDuration(s.seconds_remaining)
      : "—";
  }

  function renderLog(s) {
    if (!s.log) return;
    els.log.innerHTML = "";
    const items = [...s.log].reverse();
    for (const entry of items) {
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="ts">${fmtTime(entry.t)}</span>
        <span class="level ${entry.level}">${entry.level}</span>
        <span class="msg"></span>
      `;
      li.querySelector(".msg").textContent = entry.message;
      els.log.appendChild(li);
    }
  }

  function renderTrades(s) {
    if (!s.trades || s.trades.length === 0) {
      els.trades.innerHTML = `<tr><td colspan="10" class="empty">No trades yet — waiting for trigger.</td></tr>`;
      return;
    }
    const rows = s.trades.map((t) => {
      const pnlCls = t.pnl == null ? "" : t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
      const pnlText = t.pnl == null ? "—" : fmtSignedMoney(t.pnl);
      return `
        <tr>
          <td>#${t.id}</td>
          <td class="mono">${t.window_slug}</td>
          <td><span class="tag ${t.side.toLowerCase()}">${t.side}</span></td>
          <td>${Number(t.price).toFixed(4)}</td>
          <td>${Number(t.shares).toFixed(2)}</td>
          <td>${fmtMoney(t.cost)}</td>
          <td><span class="tag ${t.status}">${t.status}</span></td>
          <td class="${pnlCls}">${pnlText}</td>
          <td><span class="tag ${t.mode}">${t.mode}</span></td>
          <td class="mono">${t.order_id || "—"}</td>
        </tr>
      `;
    });
    els.trades.innerHTML = rows.join("");
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

    // Background grid
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = (i / 4) * cssH;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(cssW, y);
      ctx.stroke();
    }

    // Trigger line
    const trigger = s.trigger_price;
    if (trigger && trigger > 0 && trigger < 1) {
      const y = cssH - trigger * cssH;
      ctx.strokeStyle = "rgba(241,180,76,0.7)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(cssW, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(241,180,76,0.9)";
      ctx.font = "11px ui-monospace,monospace";
      ctx.fillText("trigger " + trigger.toFixed(2), 6, y - 4);
    }

    const drawSeries = (points, color) => {
      if (!points || points.length < 2) return;
      const ts = points.map((p) => p.t);
      const minT = ts[0];
      const maxT = ts[ts.length - 1];
      const span = Math.max(1, maxT - minT);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((p, i) => {
        const x = ((p.t - minT) / span) * cssW;
        const y = cssH - Math.min(1, Math.max(0, p.p)) * cssH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };

    const ph = s.price_history || {};
    drawSeries(ph.up, "#1cc88a");
    drawSeries(ph.down, "#e74a3b");
  }

  async function poll() {
    try {
      const resp = await fetch("/api/state", { cache: "no-store" });
      if (!resp.ok) return;
      const s = await resp.json();
      renderBadges(s);
      renderKpis(s);
      renderWindow(s);
      renderLog(s);
      renderTrades(s);
      renderChart(s);
    } catch (e) {
      // ignore transient errors
    }
  }

  poll();
  setInterval(poll, 1000);
})();
