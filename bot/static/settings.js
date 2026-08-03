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

  /* Martingale preview. ×1.5 compounds fast — six losses is 11× the base bet —
   * so the progression is spelled out rather than left to the imagination. */
  function updateMartingalePreview() {
    const preview = $("martingale-preview");
    if (!preview) return;
    const mult = parseFloat($("cfg-martingale").value) || 1.5;
    const base = parseFloat($("cfg-fade-bet").value) || 5.0;

    const steps = [];
    let bet = base;
    let total = 0;
    for (let i = 1; i <= 6; i++) {
      steps.push(`$${bet.toFixed(2)}`);
      total += bet;
      bet *= mult;
    }
    preview.innerHTML =
      `<strong>Progresión ×${mult.toFixed(2)} desde $${base.toFixed(2)}:</strong><br/>` +
      steps.join(" → ") +
      `<br/><span class="text-muted">6 pérdidas seguidas exponen $${total.toFixed(2)}</span>`;
  }
  ["cfg-martingale", "cfg-fade-bet"].forEach((id) => {
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
      num("cfg-martingale", c.ss_martingale_mult_factor, 1.5);
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
