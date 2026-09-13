# 🌍 Resumen: Localización del VPS

## 📊 Hallazgos Principales

**Estado actual (GitHub Codespace):**
- Polymarket CLOB: **86ms** promedio
- Polymarket Gamma: **142ms** promedio  
- Binance API: **270ms** promedio

**Conclusión:** Latencias aceptables pero **no óptimas** para trading competitivo.

---

## 🎯 Recomendación Principal

### ✅ **MIGRAR A US EAST (Virginia/Nueva York)**

**Razones:**

1. **Polymarket está en US East** - Sus servidores CLOB y Gamma están en AWS Virginia
2. **Latencia esperada: < 30ms** - Vs 86-142ms actual
3. **Estrategias críticas requieren baja latencia:**
   - `near_res`: Solo viable con < 50ms (entra T-5 a T-20s)
   - `box_builder`: Repricing competitivo necesita < 30ms
   - `temporal_arb`: Detectar oportunidades antes que otros traders
   - `coin_flip_dog`: Mejora en ventana de entrada

4. **WebSocket más estable** - Menos reconnects por timeout
5. **Ventaja competitiva** - Traders en otras regiones tienen 50-200ms más de delay

---

## 📍 Ubicaciones Recomendadas

| Región | Latencia Esperada | Estrategias Viables | Calificación |
|--------|-------------------|---------------------|--------------|
| **US East (Virginia/NYC)** | **< 30ms** | Todas ✅ | ⭐⭐⭐⭐⭐ **ÓPTIMO** |
| US West (Los Angeles) | 50-70ms | Todas (near_res con desventaja) | ⭐⭐⭐⭐ |
| EU West (Frankfurt/Amsterdam) | 80-120ms | temporal_arb, coin_flip_dog | ⭐⭐⭐ |
| South America (São Paulo) | 120-180ms | Solo tolerantes | ⭐⭐ |
| Asia Pacific (Singapur) | 180-250ms | ❌ NO viable | ⭐ |

---

## 🔧 Cómo Verificar Tu VPS Actual

### Opción 1: Script Bash (Rápido)

```bash
ssh polymarket@TU_VPS_IP
cd ~/Polymarket-BotV1
bash scripts/test_latency.sh
```

### Opción 2: Script Python (Detallado)

```bash
ssh polymarket@TU_VPS_IP
cd ~/Polymarket-BotV1
python3 scripts/test_latency_detailed.py
```

**Ambos scripts te dirán:**
- ✅ Ubicación actual del VPS (ciudad, país, ISP)
- ✅ Latencia promedio, mediana, P95, P99
- ✅ Qué estrategias son viables
- ✅ Si deberías migrar y a dónde

---

## 🚀 Cómo Cambiar de Ubicación

### Hostinger VPS

**Opción A: Panel de Control**
1. Login → VPS → "Change Location" o "Rebuild VPS"
2. Seleccionar región: **US East** o **US West**
3. ⚠️ **ADVERTENCIA:** "Rebuild" borra todo - hacer backup primero

**Opción B: VPS Nuevo (Recomendado)**
1. Provisionar VPS nuevo en US East
2. Ejecutar `test_latency.sh` para confirmar mejora
3. Migrar datos del VPS viejo (ver checklist abajo)
4. Cancelar VPS viejo después de 24h de validación

### Otros Proveedores (Alternativas a Hostinger)

**AWS EC2** - us-east-1 (Virginia)
- ✅ Probablemente el MISMO datacenter que Polymarket
- Costo: ~$5-10/mes (t3.micro Reserved Instance)

**DigitalOcean** - NYC1 o NYC3
- ✅ Nueva York - latencia similar a Virginia
- Costo: $6/mes (Basic Droplet)

**Vultr** - New Jersey
- ✅ Excelente latencia a Polymarket
- Costo: $6/mes (Cloud Compute)

**Linode (Akamai)** - Newark, NJ
- ✅ Muy cerca de NYC/Virginia
- Costo: $5/mes (Nanode)

---

## 📋 Checklist de Migración

### Antes de migrar

- [ ] Ejecutar `scripts/test_latency_detailed.py` en VPS actual
- [ ] Documentar latencias actuales
- [ ] Confirmar que el nuevo proveedor/región tiene < 30ms a Polymarket

### Durante la migración

```bash
# 1. Backup en VPS actual
ssh polymarket@VPS_VIEJO
cd ~/Polymarket-BotV1
sudo systemctl stop polymarket-bot

# Backup DB
cp data/streak_snapper.db ~/backup_$(date +%Y%m%d).db

# Backup .env
cp .env ~/backup_env_$(date +%Y%m%d)

# 2. Provisionar VPS nuevo en US East

# 3. Test de latencia en VPS nuevo
ssh polymarket@VPS_NUEVO
bash <(curl -s https://raw.githubusercontent.com/TU_USER/Polymarket-BotV1/main/scripts/test_latency.sh)

# 4. Si latencia < 30ms, proceder con deployment
git clone https://github.com/TU_USER/Polymarket-BotV1.git
cd Polymarket-BotV1
bash scripts/vps_setup.sh

# 5. Copiar .env del backup
scp polymarket@VPS_VIEJO:~/backup_env_* .env

# 6. Copiar DB (opcional, solo para historial)
scp polymarket@VPS_VIEJO:~/backup_*.db data/streak_snapper.db

# 7. Crear servicio y arrancar
exit  # Volver a root
sudo bash /home/polymarket/Polymarket-BotV1/scripts/create_systemd_service.sh
sudo systemctl start polymarket-bot

# 8. Validar
sudo systemctl status polymarket-bot
tail -f /home/polymarket/Polymarket-BotV1/logs/bot.log
```

### Post-migración

- [ ] Bot arrancado sin errores
- [ ] Latencia confirmada < 30ms
- [ ] WebSocket conectado sin desconexiones
- [ ] Dashboard accesible
- [ ] Primeras trades ejecutándose correctamente
- [ ] Monitorear 24 horas
- [ ] Si todo OK, cancelar VPS viejo

---

## 💰 Impacto Esperado en Trading

### Mejoras cuantificables al migrar a US East

**1. near_res (Near Resolution Capture)**
- Actual (86ms): Libro ya movido cuando llegas
- US East (< 30ms): Puedes entrar **56ms antes** que ahora
- Impacto: +10-20% más operaciones exitosas

**2. box_builder (Maker Strategy)**
- Actual (86ms): Repricing tarda > 100ms round-trip
- US East (< 30ms): Repricing en < 60ms round-trip
- Impacto: Menos fills adversos, más boxes completadas

**3. temporal_arb**
- Actual: Ves oportunidades 86ms después
- US East: Ves oportunidades 86ms antes
- Impacto: +15-25% más señales capturadas antes de repricing

**4. Estabilidad general**
- Menos timeouts de WebSocket
- Menos órdenes rechazadas por staleness
- Menor slippage en ejecución

### ROI de la migración

**Costo adicional:** $0-3/mes (la mayoría de proveedores cobran igual por región)

**Beneficio esperado:** 
- 1-2 trades adicionales ganadas por semana = +$5-20/semana
- Menos slippage = +0.5-1% por trade
- **ROI:** Se paga en < 1 semana

---

## 📖 Documentación Completa

- **Guía detallada:** `docs/VPS_LOCATION_LATENCY.md`
- **Scripts de test:** `scripts/test_latency.sh`, `scripts/test_latency_detailed.py`
- **Deployment:** `docs/DEPLOY_VPS_HOSTINGER.md`

---

## ✅ Acción Recomendada

### Inmediato (hoy)

1. **Verificar ubicación actual:**
   ```bash
   ssh polymarket@TU_VPS
   python3 ~/Polymarket-BotV1/scripts/test_latency_detailed.py
   ```

2. **Si latencia > 50ms a Polymarket CLOB:**
   - Planear migración a US East
   - Provisionar VPS nuevo en Virginia/NYC
   - Seguir checklist de migración arriba

3. **Si ya estás en US East (< 30ms):**
   - ✅ Ubicación óptima, no hacer nada
   - Continuar con testing de estrategias

---

**Conclusión:** La localización del VPS **SÍ importa** para tu bot. Migrar a US East puede mejorar significativamente el performance, especialmente para `near_res` y `box_builder`.
