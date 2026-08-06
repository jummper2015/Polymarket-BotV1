/* Chart.js panels: equity curve, drawdown, win rate and P&L per strategy.
 *
 * Data comes from /api/metrics/series, which is computed over resolved trades
 * in SQL. Refreshed far more slowly than /state — these only change when a
 * trade resolves, i.e. every 5 minutes at most, so polling them at 1 Hz would
 * be 300 wasted queries per change.
 */
(function () {
  const { $, COLORS, STRAT_LABELS, STRAT_COLORS, fmtMoney, fmtSigned, fmtPct } = window.SS;

  const REFRESH_MS = 30000;

  const charts = {};
  let equityMode = "global";
  let lastSeries = null;

  const fmtAxisTime = (t) =>
    new Date(t * 1000).toLocaleString("es-ES",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

  /* Shared options. `parsing:false` plus {x,y} points lets Chart.js skip its
   * normalisation pass, which matters once the curve holds thousands of trades. */
  const baseOptions = (extra) => Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: true, labels: { boxWidth: 10, font: { size: 11 } } },
      tooltip: { titleFont: { size: 11 }, bodyFont: { size: 11 } },
    },
    scales: {
      x: {
        type: "linear",
        ticks: {
          maxTicksLimit: 6, font: { size: 10 }, color: COLORS.muted,
          callback: (v) => fmtAxisTime(v),
        },
        grid: { color: COLORS.border },
      },
      y: {
        ticks: { font: { size: 10 }, color: COLORS.muted },
        grid: { color: COLORS.border },
      },
    },
  }, extra || {});

  const toXY = (points, field) =>
    (points || []).map((p) => ({ x: p.t, y: p[field] }));

  function upsert(id, config) {
    const el = $(id);
    if (!el) return;
    if (charts[id]) {
      // Swap the data in place; rebuilding the chart would drop tooltip state
      // and flash on every refresh.
      charts[id].data = config.data;
      charts[id].options = config.options;
      charts[id].update("none");
    } else {
      charts[id] = new Chart(el.getContext("2d"), config);
    }
  }

  function renderEquity(series) {
    const datasets = [];

    if (equityMode === "strategy") {
      const byStrategy = series.equity_by_strategy || {};
      Object.keys(byStrategy).sort().forEach((key) => {
        datasets.push({
          label: STRAT_LABELS[key] || key,
          data: toXY(byStrategy[key], "equity"),
          borderColor: STRAT_COLORS[key] || COLORS.muted,
          backgroundColor: "transparent",
          borderWidth: 2, pointRadius: 0, tension: 0.15,
        });
      });
    } else {
      datasets.push({
        label: "Capital",
        data: toXY(series.equity, "equity"),
        borderColor: COLORS.ok,
        backgroundColor: "rgba(0,194,146,.08)",
        borderWidth: 2, pointRadius: 0, tension: 0.15, fill: true,
      });
    }

    upsert("chart-equity", {
      type: "line",
      data: { datasets },
      options: baseOptions({
        plugins: {
          legend: { display: datasets.length > 1, labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              title: (items) => fmtAxisTime(items[0].parsed.x),
              label: (ctx) => `${ctx.dataset.label}: ${fmtMoney(ctx.parsed.y)}`,
            },
          },
        },
      }),
    });
  }

  function renderDrawdown(series) {
    upsert("chart-drawdown", {
      type: "line",
      data: {
        datasets: [{
          label: "Drawdown",
          data: toXY(series.drawdown, "drawdown"),
          borderColor: COLORS.err,
          backgroundColor: "rgba(228,106,118,.12)",
          borderWidth: 2, pointRadius: 0, tension: 0.15, fill: true,
        }],
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => fmtAxisTime(items[0].parsed.x),
              label: (ctx) => "Caída desde máximo: " + fmtMoney(ctx.parsed.y),
            },
          },
        },
        scales: {
          x: baseOptions().scales.x,
          // Inverted so a deeper drawdown reads as further down, the way it feels.
          y: {
            reverse: true,
            ticks: { font: { size: 10 }, color: COLORS.muted,
                     callback: (v) => "$" + Number(v).toFixed(0) },
            grid: { color: COLORS.border },
          },
        },
      }),
    });
  }

  function renderWinRate(series) {
    const pct = (points, field) =>
      (points || []).map((p) => ({ x: p.t, y: p[field] * 100 }));

    upsert("chart-winrate", {
      type: "line",
      data: {
        datasets: [
          {
            label: "Acumulado",
            data: pct(series.win_rate, "cumulative"),
            borderColor: COLORS.trend,
            backgroundColor: "transparent",
            borderWidth: 2, pointRadius: 0, tension: 0.15,
          },
          {
            label: `Móvil (${series.rolling_window || 50})`,
            data: pct(series.win_rate, "rolling"),
            borderColor: COLORS.warn,
            backgroundColor: "transparent",
            borderWidth: 1.5, pointRadius: 0, tension: 0.15, borderDash: [4, 3],
          },
        ],
      },
      options: baseOptions({
        plugins: {
          legend: { display: true, labels: { boxWidth: 10, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              title: (items) => fmtAxisTime(items[0].parsed.x),
              label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}%`,
            },
          },
        },
        scales: {
          x: baseOptions().scales.x,
          y: {
            min: 0, max: 100,
            ticks: { font: { size: 10 }, color: COLORS.muted, callback: (v) => v + "%" },
            grid: { color: COLORS.border },
          },
        },
      }),
    });
  }

  /* P&L per strategy comes from /state (aggregated over every trade in SQL),
   * not from the series — the series is capped and would undercount. */
  function renderStrategyPnl(stats) {
    const keys = Object.keys(stats || {});
    if (!keys.length) return;

    const values = keys.map((k) => (stats[k] || {}).pnl || 0);

    upsert("chart-strategy", {
      type: "bar",
      data: {
        labels: keys.map((k) => STRAT_LABELS[k] || k),
        datasets: [{
          label: "P&L",
          data: values,
          backgroundColor: values.map((v) => (v >= 0 ? COLORS.ok : COLORS.err)),
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => fmtSigned(ctx.parsed.y) } },
        },
        scales: {
          x: { grid: { display: false }, ticks: { font: { size: 11 }, color: COLORS.muted } },
          y: {
            ticks: { font: { size: 10 }, color: COLORS.muted,
                     callback: (v) => "$" + Number(v).toFixed(0) },
            grid: { color: COLORS.border },
          },
        },
      },
    });
  }

  function renderDrawdownKpi(series) {
    const abs = $("kpi-drawdown");
    if (abs) abs.textContent = series.max_drawdown ? fmtMoney(series.max_drawdown) : "—";
    const pct = $("kpi-drawdown-pct");
    if (pct) {
      pct.textContent = series.max_drawdown
        ? fmtPct(series.max_drawdown_pct) + " del máximo"
        : "sin caídas";
    }
  }

  /* The charts follow the asset tab. Without this the equity curve under an
   * ETH tab was every market's P&L added together, which is a portfolio line
   * dressed up as a per-market one. `ss_symbol` is written by dashboard.js. */
  function symbolQuery() {
    try {
      const sym = localStorage.getItem("ss_symbol");
      return sym ? "?symbol=" + encodeURIComponent(sym) : "";
    } catch (_) {
      return "";
    }
  }

  async function loadSeries() {
    try {
      const resp = await fetch("/api/metrics/series" + symbolQuery(),
                               { cache: "no-store" });
      if (!resp.ok) return;
      const series = await resp.json();
      lastSeries = series;

      if (!series.equity || !series.equity.length) {
        // Nothing resolved yet — leave the canvases blank rather than drawing
        // an empty pair of axes that looks like a broken chart.
        return;
      }

      renderEquity(series);
      renderDrawdown(series);
      renderWinRate(series);
      renderDrawdownKpi(series);

      if (series.truncated) {
        const note = $("winrate-note");
        if (note) note.textContent = `mostrando las últimas ${series.resolved_trades} operaciones`;
      }
    } catch (_) {
      /* Charts are secondary; a failure here must not disturb the live panel. */
    }
  }

  async function loadStrategyPnl() {
    try {
      const resp = await fetch("/state" + symbolQuery(), { cache: "no-store" });
      if (!resp.ok) return;
      const s = await resp.json();
      renderStrategyPnl(s.strategy_stats);
    } catch (_) {}
  }

  document.querySelectorAll("[data-equity-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-equity-mode]")
        .forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      equityMode = btn.dataset.equityMode;
      if (lastSeries) renderEquity(lastSeries);
    });
  });

  function refreshAll() {
    loadSeries();
    loadStrategyPnl();
  }

  refreshAll();
  setInterval(() => { if (!document.hidden) refreshAll(); }, REFRESH_MS);
})();
