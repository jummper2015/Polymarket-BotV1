# 🔍 PRODUCTION_AUDIT — Auditoría pre-merge del bot

> Adaptado de la skill `production-audit` de ECC.
> **No envía el repo a servicios externos** — toda la evidencia se recoge
> localmente (codespace + VPS vía SSH). Pensado para ejecutarse **antes de
> cada `git push` que toque código de runtime** (`bot/`, `main.py`,
> `tests/`) y **antes de cada deploy al VPS**.

---

## 1. Cuándo ejecutar

- ✅ Antes de cada merge a `main` que toque `bot/`, `main.py`, `templates/`,
  `static/`, o `tests/`
- ✅ Antes de cada scp al VPS que cambie archivos de runtime (siguiendo
  `docs/PAPER_TO_REAL_RUNBOOK.md`)
- ✅ Después de un incidente en producción (post-mortem)
- ✅ Cuando el operador pregunta "¿está listo para producción?" o
  "¿qué se rompería en prod?"
- ❌ NO durante implementación activa (usar `code-review` o `security-review`
  en su lugar)
- ❌ NO para auditoría legal/financiera/regulatoria (esto es triage de
  ingeniería, no certificación)

---

## 2. Workflow

### Paso 1 — Evidencia local (codespace)

Desde el directorio del repo:

```bash
# Working tree limpio?
git status --short
# Esperado: nothing to commit, working tree clean

# Todos los tests verdes?
python -m pytest tests/ -q
# Esperado: 599+ passed (3 fallas pre-existentes en test_spread_harvest.py
# son conocidas — ver sección 5 abajo)

# No credenciales filtradas?
git ls-files | grep -E '\.env$|\.db$|PRIVATE_KEY|SECRET|PASSPHRASE'
# Esperado: 0 líneas

# Smoke import — el bot arranca sin errores de sintaxis?
python -c "import bot.main, bot.state, bot.streak_trader, bot.config, bot.db"
# Esperado: sin output
```

### Paso 2 — Evidencia del VPS (SSH read-only)

```bash
export SSHPASS='...'   # set via env, no en history
sshpass -e ssh root@76.13.251.202 '
  echo "--- systemd unit ---"
  systemctl is-active polymarket-bot
  echo "--- geoblock desde VPS ---"
  curl -s https://polymarket.com/api/geoblock
  echo
  echo "--- últimas líneas log ---"
  tail -5 /opt/polymarket-bot/logs/bot.log
  echo "--- balance poller errors (debe estar en 0 en paper) ---"
  wc -c /opt/polymarket-bot/logs/bot.error.log
  echo "--- db last trade ---"
  sqlite3 /opt/polymarket-bot/data/streak_snapper.db \
    "SELECT id, timestamp, strategy, pnl FROM trades ORDER BY id DESC LIMIT 1;"
'
```

Resultados esperados:
- `systemctl is-active polymarket-bot` → `active`
- Geoblock → `{"blocked":false,"ip":"..."}` o 200 OK con country LT
- `bot.error.log` → `0` bytes (paper mode) o tamaño estable
- DB tiene trades recientes (bot está vivo)

### Paso 3 — Diff vs producción

```bash
# Local HEAD vs VPS HEAD (working tree)
git log --oneline -1   # local
# VPS:
#   cd /opt/polymarket-bot && git log --oneline -1

# Listar archivos que difieren en contenido entre local y VPS
md5sum bot/*.py bot/**/*.py | sort > /tmp/local.md5
sshpass -e ssh root@76.13.251.202 \
  'cd /opt/polymarket-bot && find bot -type f -name "*.py" | xargs md5sum | sort' \
  > /tmp/vps.md5
diff /tmp/local.md5 /tmp/vps.md5
```

Si diff está vacío, el working tree local == VPS working tree para los `*.py`.

### Paso 4 — Checklist específico del bot

| Item | Comando / Verificación | Esperado | OK? |
|---|---|---|---|
| Geoblock desde VPS | `curl https://polymarket.com/api/geoblock` | no bloqueado | [ ] |
| Systemd unit | `systemctl is-active polymarket-bot` | `active` | [ ] |
| `bot.error.log` | `wc -c logs/bot.error.log` | 0 bytes (paper) | [ ] |
| `bot.log` rotación | `ls logs/bot.log.*.gz` | ≥1 archivo rotado reciente | [ ] |
| DB accesible | `sqlite3 data/streak_snapper.db "SELECT 1"` | `1` | [ ] |
| Last trade <5min | `sqlite3 ... 'SELECT MAX(timestamp) FROM trades'` | < 300s | [ ] |
| `.env` no commiteado | `git ls-files \| grep '\.env$'` | vacío | [ ] |
| Tests | `pytest tests/ -q` | 599+ pass | [ ] |
| Smoke import | `python -c "import bot.main"` | OK | [ ] |
| Working tree limpio | `git status` | clean | [ ] |
| `DASHBOARD_HOST` | `grep DASHBOARD_HOST .env` | `127.0.0.1` | [ ] |
| `DASHBOARD_PASSWORD` | `grep DASHBOARD_PASSWORD .env` | non-empty | [ ] |
| `TRADING_MODE` | `grep TRADING_MODE .env` | `paper` (o `real` si firmado) | [ ] |
| `PROXY_WALLET` | `grep PROXY_WALLET .env` | non-empty | [ ] |

### Paso 5 — Pre-deploy gates

Antes de `scp` al VPS:

- [ ] Step 1 (evidencia local) ✅
- [ ] Step 2 (evidencia VPS) ✅
- [ ] Step 3 (diff vs prod) ✅
- [ ] Step 4 checklist completo ✅
- [ ] Backup de los archivos que vas a tocar en VPS:
      `ssh root@76.13.251.202 'mkdir -p /opt/polymarket-bot/.backups/audit_<TS>'`
- [ ] Si hay credenciales nuevas en `.env` (L2 keys regeneradas), verificar
      `scripts/generate_api_keys.py` se ejecutó correctamente

### Paso 6 — Deploy + smoke test

Siguiendo `docs/PAPER_TO_REAL_RUNBOOK.md`:

```bash
# scp archivos modificados (NO el repo entero, NO .env)
scp bot/file.py root@76.1:/opt/polymarket-bot/bot/

# Restart
ssh root@76.1 'systemctl restart polymarket-bot'

# Verificar arranque
ssh root@76.1 'sleep 5 && tail -20 /opt/polymarket-bot/logs/bot.log'
```

Resultado esperado: log muestra startup line + primer trade observando.

### Paso 7 — Post-deploy verification

Después de 5-10 minutos:

- [ ] No errores nuevos en `bot.error.log`
- [ ] Al menos 1 trade en DB (bot está ejecutando señales)
- [ ] Dashboard accesible en `https://polytradebot.cloud/dashboard`
- [ ] `/settings` permite editar y persiste

---

## 3. Fallas pre-existentes conocidas

**`tests/test_spread_harvest.py` tiene 3 fallas estables** que existen
tanto en local HEAD como en VPS (verificado por md5 idéntico y por re-run
del test desde el VPS con el mismo resultado):

```
FAILED test_a_quotable_window_is_counted_once
FAILED test_a_tight_book_window_is_counted_as_such
FAILED test_one_quotable_tick_is_enough
```

Estas fallas **NO son bloqueantes** porque `spread_harvest` está en la lista de
estrategias desactivadas (ver `CLAUDE.md` §"Desactivadas"). El módulo existe
como referencia histórica pero no se ejecuta. Tratar como technical debt,
no como regression.

**Acción recomendada**: limpiar las assertions obsoletas o borrar
`bot/strategies/spread_harvest.py` y `tests/test_spread_harvest.py`
completamente en una PR de cleanup aparte.

---

## 4. Output Contract

Resultado de ejecutar este audit:

1. **commit hash auditado**: `__________`
2. **fecha**: `__________`
3. **evidencia local**: tests + smoke + git status + grep credenciales
4. **evidencia VPS**: systemd + geoblock + logs + DB
5. **diff vs prod**: archivos modificados (lista)
6. **checklist pre-deploy**: ✅/❌ por item
7. **acción**: deploy | posponer | rollback

Si **cualquier** item es ❌, **NO desplegar** hasta resolver.

---

## 5. Cuándo NO deployear

- Tests rojos (más allá de las 3 fallas pre-existentes)
- `bot.error.log` con errores nuevos
- Diff vs VPS muestra archivos modificados que NO planeas deployar
- Geoblock reporta bloqueado
- `DASHBOARD_PASSWORD` vacío con `DASHBOARD_HOST != 127.0.0.1`
- Working tree sucio (cambios sin commitear)
- `.env` aparece en `git ls-files`

---

## 6. Incidente post-deploy

Si algo se rompe después del deploy:

```bash
# 1. Snapshot del estado actual
ssh root@76.1 'cp /opt/polymarket-bot/logs/bot.log /tmp/incident_<TS>.log'

# 2. Rollback desde el backup pre-deploy
ssh root@76.1 'cp -r /opt/polymarket-bot/.backups/audit_<TS>/* /opt/polymarket-bot/'

# 3. Restart
ssh root@76.1 'systemctl restart polymarket-bot'

# 4. Verificar
ssh root@76.1 'sleep 5 && tail -20 /opt/polymarket-bot/logs/bot.log'
```

Luego post-mortem: ¿qué pasó?, ¿qué lo detectó tarde?, ¿qué agregar al
checklist?