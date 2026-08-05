/* Settings page.
 *
 * Was ~160 lines inline in settings.html, which meant it shared nothing with
 * dashboard.js and re-declared its own helpers. Lives here now.
 *
 * Loads from /state and saves through POST /config, which validates every field
 * against RUNTIME_FIELDS server-side — this file only has to send sane types.
 */
(function () {
  const $ = (id) => document.getElementById(id);

  let selectedMode = "paper";   // paper | real
  let ssMode = "both";          // fade | trend | both

  // ── strategy mode selector ──
  function selectSSMode(mode) {
    ssMode = mode;
    document.querySelectorAll(".ss-mode-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.mode === mode);
    });
    const show = (id, visible) => {
      const el = $(id);
      if (el) el.style.display = visible ? "" : "none";
    };
    show("fade-config-section", mode === "fade" || mode === "both");
    show("trend-config-section", mode === "trend" || mode === "both");
  }

  document.querySelectorAll(".ss-mode-card").forEach((card) => {
    card.addEventListener("click", () => selectSSMode(card.dataset.mode));
  });

  // ── enable toggle ──
  const ssCheck = $("cfg-ss-enabled");
  const ssLabel = $("ss-enabled-label");
  if (ssCheck && ssLabel) {
    ssCheck.addEventListener("change", () => {
      ssLabel.textContent = ssCheck.checked ? "Activado" : "Desactivado";
    });
  }

  // ── trading mode ──
  function setModeUI(mode) {
    selectedMode = mode;
    $("btn-paper").classList.toggle("active", mode === "paper");
    $("btn-real").classList.toggle("active", mode === "real");
  }
  $("btn-paper").addEventListener("click", () => setModeUI("paper"));
  $("btn-real").addEventListener("click", () => setModeUI("real"));

  /* Martingale preview.
   *
   * The number that matters isn't the size of the next bet, it's what you're
   * left with if that bet WINS. Buying `s` shares at `p` costs `s·p` and pays
   * `s`, so a win only clears the cycle while factor > 1/(1-p). Below that the
   * accumulated losses outgrow the payout and "keep going until you win" ends
   * in a bigger hole. The old preview showed only the progression, which made
   * ×1.5 at a 0.52 cap look survivable — it isn't. */
  function updateMartingalePreview() {
    const preview = $("martingale-preview");
    if (!preview) return;
    const mult = parseFloat($("cfg-martingale").value) || 2.1;
    const base = parseFloat($("cfg-fade-bet").value) || 5.0;
    const cap = parseFloat($("cfg-trend-cap").value) || 0.52;

    const needed = cap < 1 ? 1 / (1 - cap) : Infinity;
    const rows = [];
    let shares = base;
    let spent = 0;
    let firstNegative = 0;

    for (let i = 1; i <= 6; i++) {
      const cost = shares * cap;
      const net = shares - cost - spent;   // payout − this cost − everything lost so far
      if (net < 0 && !firstNegative) firstNegative = i;
      rows.push(
        `<td>${i}</td><td>${shares.toFixed(1)} sh</td>` +
        `<td>$${(spent + cost).toFixed(2)}</td>` +
        `<td class="${net < 0 ? "text-danger" : "text-success"}">` +
        `${net >= 0 ? "+" : ""}$${net.toFixed(2)}</td>`
      );
      spent += cost;
      shares *= mult;
    }

    const verdict =
      mult > needed
        ? `<span class="text-success">×${mult.toFixed(2)} recupera a $${cap.toFixed(2)}` +
          ` (hace falta más de ×${needed.toFixed(2)}).</span>`
        : `<span class="text-danger"><strong>×${mult.toFixed(2)} NO recupera a ` +
          `$${cap.toFixed(2)}</strong>: hace falta más de ×${needed.toFixed(2)}. ` +
          `Ganar el intento ${firstNegative || 3} ya deja el ciclo en pérdida.</span>`;

    preview.innerHTML =
      `<strong>Ciclo a precio máximo $${cap.toFixed(2)} desde ${base.toFixed(1)} shares:</strong>` +
      `<table class="table table-sm small mb-2 mt-1"><thead><tr>` +
      `<th>#</th><th>Apuesta</th><th>Acumulado</th><th>Neto si gana</th>` +
      `</tr></thead><tbody><tr>${rows.join("</tr><tr>")}</tr></tbody></table>` +
      verdict;
  }
  ["cfg-martingale", "cfg-fade-bet", "cfg-trend-cap"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", updateMartingalePreview);
  });

  const num = (id, value, fallback) => {
    const el = $(id);
    if (el) el.value = value != null ? value : fallback;
  };
  const check = (id, value) => {
    const el = $(id);
    if (el) el.checked = !!value;
  };

  // ── load ──
  async function loadState() {
    try {
      const resp = await fetch("/state", { cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) return;

      const s = await resp.json();
      const c = s.config || {};

      if (ssCheck) {
        ssCheck.checked = c.ss_enabled !== false;
        ssLabel.textContent = ssCheck.checked ? "Activado" : "Desactivado";
      }
      selectSSMode(c.ss_mode || "both");

      num("cfg-fade-bet", c.ss_fade_base_shares, 5.0);
      num("cfg-fade-cap", c.ss_fade_limit_cap, 0.60);
      num("cfg-fade-streak", c.ss_fade_streak_min, 4);
      num("cfg-trend-bet", c.ss_trend_base_shares, 5.0);
      num("cfg-trend-cap", c.ss_trend_limit_cap, 0.52);
      // Stored as a fraction, shown as a percentage.
      num(
        "cfg-trend-strength",
        c.ss_trend_min_strength != null
          ? +(c.ss_trend_min_strength * 100).toFixed(3)
          : null,
        0.8,
      );
      num("cfg-martingale", c.ss_martingale_mult_factor, 2.1);
      num("cfg-bankroll", c.starting_bankroll, 1000);

      check("cfg-cl-enabled", c.cl_twap_enabled);
      check("cfg-cl-record", c.cl_record_ticks);
      const win = $("cfg-cl-window");
      if (win) win.value = String(c.cl_twap_window || 30);
      num("cfg-cl-stale", c.cl_twap_stale_seconds, 15);
      num("cfg-cl-divergence", c.cl_divergence_max, 0);

      setModeUI(c.mode || "paper");
      updateMartingalePreview();

      const badge = $("mode-badge");
      if (badge) {
        badge.textContent = (c.mode || "paper").toUpperCase();
        badge.className = "ss-badge " + (c.mode === "real" ? "real" : "paper");
      }

      renderReadiness(s.real_mode_readiness);
    } catch (_) {
      showMsg("No se pudo cargar la configuración", "text-danger");
    }
  }

  function renderReadiness(r) {
    if (!r) return;
    const badge = $("readiness-badge");
    if (badge) {
      badge.textContent = r.ready ? "✓ Listo" : "✗ Pendiente";
      badge.className = "ss-badge " + (r.ready ? "ok" : "warn");
    }
    const steps = $("readiness-steps");
    if (!steps) return;

    const item = (done, label, detail) => `
      <li class="d-flex gap-2 mb-2">
        <i class="bi ${done ? "bi-check-circle-fill text-success" : "bi-circle text-muted"}"></i>
        <div>
          <strong>${label}</strong>
          ${detail ? `<div class="ss-field-hint">${detail}</div>` : ""}
        </div>
      </li>`;

    steps.innerHTML =
      item(r.has_private_key, "PRIVATE_KEY configurada") +
      item(r.has_proxy_wallet, "PROXY_WALLET configurada") +
      item(false, 'Cambiar modo a "Real" y aplicar',
           "Selecciona Real arriba y guarda");
  }

  // ── save ──
  function showMsg(text, cls) {
    const el = $("cfg-save-msg");
    if (!el) return;
    el.textContent = text;
    el.className = "small mt-2 " + cls;
    setTimeout(() => { el.textContent = ""; el.className = "small mt-2"; }, 5000);
  }

  $("btn-apply").addEventListener("click", async () => {
    const body = {
      ss_enabled: ssCheck ? ssCheck.checked : true,
      ss_mode: ssMode,
      ss_fade_base_shares: parseFloat($("cfg-fade-bet").value),
      ss_fade_limit_cap: parseFloat($("cfg-fade-cap").value),
      ss_fade_streak_min: parseInt($("cfg-fade-streak").value, 10),
      ss_trend_base_shares: parseFloat($("cfg-trend-bet").value),
      ss_trend_limit_cap: parseFloat($("cfg-trend-cap").value),
      // Entered as a percentage, stored as a fraction.
      ss_trend_min_strength: parseFloat($("cfg-trend-strength").value) / 100,
      ss_martingale_mult_factor: parseFloat($("cfg-martingale").value),
      starting_bankroll: parseFloat($("cfg-bankroll").value),
      mode: selectedMode,
      cl_twap_enabled: $("cfg-cl-enabled").checked,
      cl_twap_window: $("cfg-cl-window").value,
      cl_twap_stale_seconds: parseFloat($("cfg-cl-stale").value),
      cl_divergence_max: parseFloat($("cfg-cl-divergence").value),
      cl_record_ticks: $("cfg-cl-record").checked,
    };
    // An empty input parses to NaN, which would serialise as null and be
    // rejected. Drop those instead of sending them.
    Object.keys(body).forEach((k) => {
      if (typeof body[k] === "number" && isNaN(body[k])) delete body[k];
    });

    try {
      const resp = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }

      const data = await resp.json();
      if (resp.ok && data.ok) {
        const keys = Object.keys(data.updated || {});
        showMsg("✓ Guardado: " + (keys.length ? keys.join(", ") : "sin cambios"),
                "text-success");
        loadState();
      } else {
        showMsg("✗ " + (data.error || "Error al guardar"), "text-danger");
      }
    } catch (err) {
      showMsg("✗ Error: " + err.message, "text-danger");
    }
  });

  $("btn-reset").addEventListener("click", async () => {
    if (!confirm("¿Descartar los ajustes guardados y volver a los valores del .env?")) return;
    try {
      const resp = await fetch("/config/reset", { method: "POST", cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const data = await resp.json();
      if (resp.ok && data.ok) {
        showMsg(`✓ Restaurados ${data.removed} ajustes desde el .env`, "text-success");
        loadState();
      } else {
        showMsg("✗ " + (data.error || "Error al restaurar"), "text-danger");
      }
    } catch (err) {
      showMsg("✗ Error: " + err.message, "text-danger");
    }
  });

  loadState();
})();
