/* Settings page.
 *
 * Nothing here knows the name of a parameter. /state serves `fields` (the
 * declaration of every RUNTIME_FIELD: kind, range, label, hint) and
 * `strategies` (the registry), and this file renders inputs from that and
 * collects them back generically.
 *
 * That is the point of the registry: before it, adding a parameter meant
 * editing config.py, state.py, dashboard.py, settings.html *and* this file —
 * and forgetting the last two is how ss_sizing and the regime filters ended up
 * configurable by POST but invisible on screen for a whole phase.
 *
 * POST /config re-validates everything against RUNTIME_FIELDS server-side, so
 * this file only has to send sane types.
 */
(function () {
  const $ = (id) => document.getElementById(id);

  let selectedMode = "paper";   // paper | real
  let ssMode = "fade";          // fade | trend | both
  let schema = {};              // name → field declaration, from /state.fields

  // Which base fields go in which card. Strategy parameters aren't here: they
  // come from the registry and are rendered into their own cards.
  const GROUPS = {
    "sizing-fields": ["ss_sizing", "ss_kelly_fraction"],
    "martingale-fields": ["ss_martingale_mult_factor", "starting_bankroll"],
    "regime-fields": [
      "ss_trading_hours",
      "ss_vol_min_pct",
      "ss_vol_max_pct",
      "ss_range_max_pct",
    ],
    "chainlink-fields": [
      "cl_twap_enabled",
      "cl_record_ticks",
      "cl_twap_window",
      "cl_twap_stale_seconds",
      "cl_divergence_max",
    ],
  };

  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
    );

  // ── field rendering ──────────────────────────────────────────────────────
  //
  // The id is `cfg-<field name>` and the input carries data-cfg-field, which is
  // what collect() scans for. Nothing else links a widget to a config key.
  function fieldHtml(field, width) {
    const id = "cfg-" + field.name;
    const hint = field.hint
      ? `<span class="ss-field-hint">${esc(field.hint)}</span>`
      : "";
    const common =
      `id="${id}" data-cfg-field="${field.name}" ` +
      `data-kind="${field.kind}" data-scale="${field.scale || 1}"`;

    let control;
    if (field.kind === "bool") {
      return `
        <div class="col-md-${width}">
          <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" ${common} />
            <label class="form-check-label ss-field-label" for="${id}">
              ${esc(field.label)}
            </label>
          </div>
          ${hint}
        </div>`;
    }

    if (field.kind === "choice") {
      const opts = (field.choices || [])
        .map(
          (c, i) =>
            `<option value="${esc(c)}">` +
            `${esc((field.choice_labels || field.choices)[i])}</option>`,
        )
        .join("");
      control = `<select class="form-select form-select-sm" ${common}>${opts}</select>`;
    } else if (field.kind === "hours") {
      control =
        `<input type="text" class="form-control form-control-sm" ` +
        `placeholder="13-21,21-24" ${common} />`;
    } else {
      const attrs = [
        field.step != null ? `step="${field.step}"` : 'step="any"',
        field.min != null ? `min="${field.min * (field.scale || 1)}"` : "",
        field.max != null ? `max="${field.max * (field.scale || 1)}"` : "",
      ].join(" ");
      control = `<input type="number" ${attrs} class="form-control form-control-sm" ${common} />`;
    }

    return `
      <div class="col-md-${width}">
        <label class="ss-field-label" for="${id}">${esc(field.label)}</label>
        ${control}
        ${hint}
      </div>`;
  }

  function renderGroup(containerId, names) {
    const box = $(containerId);
    if (!box) return;
    const fields = names.map((n) => schema[n]).filter(Boolean);
    const width = fields.length >= 4 ? 3 : fields.length === 3 ? 4 : 6;
    box.innerHTML = fields.map((f) => fieldHtml(f, width)).join("");
  }

  // ── strategy cards ───────────────────────────────────────────────────────
  function renderStrategies(list) {
    const box = $("strategy-cards");
    if (!box) return;

    box.innerHTML = (list || [])
      .map((s) => {
        const width = s.params.length >= 3 ? 4 : 6;
        const notes = s.notes
          ? `<div class="alert alert-info py-2 px-3 small mt-2 mb-0">${esc(s.notes)}</div>`
          : "";
        const symbols = s.symbols && s.symbols.length
          ? `<span class="ss-field-hint ms-2">solo ${s.symbols.join(", ").toUpperCase()}</span>`
          : "";
        return `
          <div class="mb-3 ss-strategy-block" data-strategy="${esc(s.id)}">
            <h6 class="ss-field-label mb-2 d-flex align-items-center">
              <span style="color:var(--ss-${s.id === "ss_trend" ? "trend" : "fade"})">
                ${esc(s.name)}
              </span>
              <span class="ss-badge ms-2" data-role="enabled-badge">—</span>
              ${symbols}
            </h6>
            <p class="ss-field-hint mb-2">${esc(s.description)}</p>
            <div class="row g-2">
              ${s.params.map((p) => fieldHtml(p, width)).join("")}
            </div>
            ${notes}
          </div>`;
      })
      .join("");
  }

  /* A strategy's card greys out the moment its toggle changes, before saving.
   * The condition comes from the descriptor (`enabled_when`), not from a copy
   * of the rule written here — a copy would go stale the first time a strategy
   * changed how it switches on, and the page would lie about what the trader
   * is doing. */
  let registry = [];
  function refreshEnabledState() {
    registry.forEach((s) => {
      const block = document.querySelector(`[data-strategy="${s.id}"]`);
      if (!block) return;

      let on = s.enabled;
      const when = s.enabled_when;
      if (when) {
        const live = readValue(when.field);
        on = when.values.some((v) => String(v) === String(live));
      }

      block.classList.toggle("ss-strategy-off", !on);
      const badge = block.querySelector('[data-role="enabled-badge"]');
      if (badge) {
        badge.textContent = on ? "activa" : "apagada";
        badge.className = "ss-badge ms-2 " + (on ? "ok" : "warn");
      }
    });
  }

  // Current value of a config key, wherever its widget lives. `ss_mode` has no
  // input of its own — it's the three cards at the top.
  function readValue(name) {
    if (name === "ss_mode") return ssMode;
    const el = $("cfg-" + name);
    if (!el) return null;
    if (el.type === "checkbox") return el.checked;
    return el.value;
  }

  // ── strategy mode selector ───────────────────────────────────────────────
  function selectSSMode(mode) {
    ssMode = mode;
    document.querySelectorAll(".ss-mode-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.mode === mode);
    });
    refreshEnabledState();
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
   * ×1.5 at a 0.52 cap look survivable — it isn't.
   *
   * It reads the *fade* cap and base: fade is the strategy that runs by
   * default. It used to read trend's, which described a cycle nobody was
   * running. */
  function updateMartingalePreview() {
    const preview = $("martingale-preview");
    if (!preview) return;
    const numOr = (name, fallback) => {
      const v = parseFloat(readValue(name));
      return isNaN(v) ? fallback : v;
    };
    const mult = numOr("ss_martingale_mult_factor", 2.1);
    const base = numOr("ss_fade_base_shares", 5.0);
    const cap = numOr("ss_fade_limit_cap", 0.52);

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

    const sizing = readValue("ss_sizing");
    const inactive =
      sizing && sizing !== "martingale"
        ? `<div class="ss-field-hint mb-1">Sizing actual: <strong>${esc(sizing)}</strong>` +
          ` — esta progresión no se está usando.</div>`
        : "";

    const verdict =
      mult > needed
        ? `<span class="text-success">×${mult.toFixed(2)} recupera a $${cap.toFixed(2)}` +
          ` (hace falta más de ×${needed.toFixed(2)}).</span>`
        : `<span class="text-danger"><strong>×${mult.toFixed(2)} NO recupera a ` +
          `$${cap.toFixed(2)}</strong>: hace falta más de ×${needed.toFixed(2)}. ` +
          `Ganar el intento ${firstNegative || 3} ya deja el ciclo en pérdida.</span>`;

    preview.innerHTML =
      inactive +
      `<strong>Ciclo a precio máximo $${cap.toFixed(2)} desde ${base.toFixed(1)} shares:</strong>` +
      `<table class="table table-sm small mb-2 mt-1"><thead><tr>` +
      `<th>#</th><th>Apuesta</th><th>Acumulado</th><th>Neto si gana</th>` +
      `</tr></thead><tbody><tr>${rows.join("</tr><tr>")}</tr></tbody></table>` +
      verdict;
  }

  // ── load ─────────────────────────────────────────────────────────────────
  async function loadState() {
    try {
      const resp = await fetch("/state", { cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) return;

      const s = await resp.json();
      const c = s.config || {};
      schema = s.fields || {};
      registry = s.strategies || [];

      renderStrategies(registry);
      Object.entries(GROUPS).forEach(([box, names]) => renderGroup(box, names));

      // Fill every rendered widget from the config payload, applying the
      // field's display scale (ss_trend_min_strength is a fraction in the DB
      // and a percentage on screen).
      document.querySelectorAll("[data-cfg-field]").forEach((el) => {
        const name = el.dataset.cfgField;
        const value = c[name];
        if (value === undefined || value === null) return;
        if (el.type === "checkbox") {
          el.checked = !!value;
        } else if (el.dataset.kind === "float" || el.dataset.kind === "int") {
          const scale = parseFloat(el.dataset.scale) || 1;
          el.value = +(value * scale).toFixed(6);
        } else {
          el.value = String(value);
        }
      });

      // Re-bind after render: the inputs these listen to didn't exist before.
      ["ss_martingale_mult_factor", "ss_fade_base_shares", "ss_fade_limit_cap", "ss_sizing"]
        .forEach((name) => {
          const el = $("cfg-" + name);
          if (el) el.addEventListener("input", updateMartingalePreview);
        });
      document.querySelectorAll("[data-cfg-field]").forEach((el) => {
        el.addEventListener("input", refreshEnabledState);
      });

      if (ssCheck) {
        ssCheck.checked = c.ss_enabled !== false;
        ssLabel.textContent = ssCheck.checked ? "Activado" : "Desactivado";
      }
      selectSSMode(c.ss_mode || "fade");
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

  // ── save ─────────────────────────────────────────────────────────────────
  function showMsg(text, cls) {
    const el = $("cfg-save-msg");
    if (!el) return;
    el.textContent = text;
    el.className = "small mt-2 " + cls;
    setTimeout(() => { el.textContent = ""; el.className = "small mt-2"; }, 5000);
  }

  // Every widget on the page, whatever card it lives in, plus the three things
  // that aren't RuntimeFields with an input: the master switch, the mode cards
  // and paper/real.
  function collect() {
    const body = {
      ss_enabled: ssCheck ? ssCheck.checked : true,
      ss_mode: ssMode,
      mode: selectedMode,
    };

    document.querySelectorAll("[data-cfg-field]").forEach((el) => {
      const name = el.dataset.cfgField;
      if (el.type === "checkbox") {
        body[name] = el.checked;
        return;
      }
      if (el.dataset.kind === "float" || el.dataset.kind === "int") {
        const scale = parseFloat(el.dataset.scale) || 1;
        const raw = parseFloat(el.value);
        // An empty input parses to NaN, which would serialise as null and be
        // rejected by POST /config. Leave it out instead.
        if (!isNaN(raw)) body[name] = raw / scale;
        return;
      }
      body[name] = el.value;
    });

    return body;
  }

  $("btn-apply").addEventListener("click", async () => {
    try {
      const resp = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect()),
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
