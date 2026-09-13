# 🌍 Localización del VPS y Consideraciones de Latencia

## 📊 Resultados de Latencia Actual (GitHub Codespace)

```
Polymarket CLOB:  86.2ms promedio (59.9ms - 140.0ms)
Polymarket Gamma: 141.5ms promedio (126.3ms - 151.3ms)
Binance API:      269.6ms promedio (257.7ms - 277.3ms)
```

**Interpretación:** Latencias aceptables pero no óptimas. El Codespace probablemente está en **US East** (Virginia/Carolina).

---

## 🎯 Infraestructura de Polymarket

### Servidores de Polymarket

Polymarket opera principalmente en infraestructura **AWS US East** (Virginia):

- **CLOB (Central Limit Order Book):** `clob.polymarket.com`
- **Gamma API:** `gamma-api.polymarket.com`
- **WebSocket Feed:** `wss://ws-subscriptions-clob.polymarket.com`
- **Polygon RPC:** Ethereum L2 distribuido globalmente

### Servidores de Binance

Binance tiene presencia global con edge servers en:
- **Asia Pacific:** Singapur, Tokio
- **Europe:** Frankfurt, Londres
- **Americas:** Virginia, São Paulo

---

## 📍 Ubicaciones Óptimas para VPS

### Opción 1: **US East Coast** (Recomendado) ⭐

**Ubicaciones específicas:**
- Virginia (US-VA)
- Nueva York (US-NY)
- Nueva Jersey (US-NJ)

**Latencia esperada:**
- Polymarket CLOB: **5-20ms** ✅
- Polymarket Gamma: **5-20ms** ✅
- Binance API: **50-80ms** ✅
- Polygon RPC: **10-30ms** ✅

**Ventajas:**
- ✅ Latencia mínima a Polymarket (mismo data center potencial)
- ✅ Mejor ejecución para estrategias taker (CFD, TA, NRC)
- ✅ WebSocket más estable
- ✅ Ventaja en box_builder para repricing rápido

**Desventajas:**
- ⚠️ Horarios US: mercados más activos 9 AM - 11 PM ET
- ⚠️ Puede ser más caro que Europa

---

### Opción 2: **Europe West**

**Ubicaciones específicas:**
- Frankfurt, Alemania (EU-FR)
- Londres, UK (EU-UK)
- Amsterdam, Países Bajos (EU-NL)

**Latencia esperada:**
- Polymarket CLOB: **80-120ms** ⚠️
- Polymarket Gamma: **80-120ms** ⚠️
- Binance API: **20-40ms** ✅
- Polygon RPC: **60-100ms** ⚠️

**Ventajas:**
- ✅ Latencia excelente a Binance
- ✅ Buen balance para temporal_arb (necesita Binance + Polymarket)
- ✅ Generalmente más barato
- ✅ GDPR compliance

**Desventajas:**
- ❌ Latencia adicional de 60-100ms a Polymarket
- ❌ Menos competitivo para near_res (cada ms cuenta)

---

### Opción 3: **Asia Pacific** (No Recomendado)

**Ubicaciones específicas:**
- Singapur (AP-SG)
- Tokio (AP-TK)

**Latencia esperada:**
- Polymarket CLOB: **180-250ms** ❌
- Polymarket Gamma: **180-250ms** ❌
- Binance API: **5-20ms** ✅

**Desventajas:**
- ❌ Latencia excesiva a Polymarket
- ❌ No viable para estrategias taker competitivas
- ❌ WebSocket puede tener más desconexiones

**Solo considerar si:**
- Operas exclusivamente en mercados asiáticos
- Tu estrategia no es time-sensitive

---

## 🏢 Opciones de Hosting por Región

### Hostinger - Ubicaciones VPS Disponibles

| Región | Ciudad | Código | Latencia Esperada | Recomendación |
|--------|--------|--------|-------------------|---------------|
| **US East** | Virginia | `us-east` | 5-20ms | ⭐⭐⭐⭐⭐ **ÓPTIMO** |
| **US West** | Los Angeles | `us-west` | 50-70ms | ⭐⭐⭐ Aceptable |
| **Europe** | Amsterdam/UK | `eu-west` | 80-120ms | ⭐⭐⭐ Bueno para TA |
| **Asia** | Singapur | `ap-sg` | 180-250ms | ⭐ No recomendado |
| **South America** | São Paulo | `sa-br` | 120-180ms | ⭐⭐ Solo si estás en SA |

### Otros Proveedores Recomendados

#### AWS EC2
- **us-east-1 (Virginia):** Probablemente el MISMO data center que Polymarket
- **us-east-2 (Ohio):** Segunda mejor opción
- Costo: ~$5-10/mes (t3.micro con Reserved Instance)

#### DigitalOcean
- **NYC1/NYC3:** Nueva York - latencia similar a Virginia
- **LON1:** Londres - buena opción europea
- Costo: $6/mes (Basic Droplet)

#### Vultr
- **New Jersey:** Excelente latencia a Polymarket
- **Miami:** Alternativa US East
- Costo: $6/mes (Cloud Compute)

#### Linode (Akamai)
- **Newark, NJ:** Muy cerca de NYC/Virginia
- **Atlanta:** Alternativa US Southeast
- Costo: $5/mes (Nanode)

---

## ⚡ Impacto de Latencia por Estrategia

### Estrategias MUY sensibles a latencia

#### 1. **near_res** (Near Resolution Capture)
- **Umbral crítico:** < 50ms
- **Razón:** Entra T-5 a T-20s; libro se mueve en milisegundos
- **Recomendación:** Solo operar con VPS en US East

#### 2. **box_builder** (repricing)
- **Umbral crítico:** < 30ms
- **Razón:** Repreciar cada `BB_REPRICE_INTERVAL`; competir con otros makers
- **Recomendación:** US East preferido, EU West aceptable

---

### Estrategias moderadamente sensibles

#### 3. **temporal_arb**
- **Umbral crítico:** < 100ms
- **Razón:** Necesita precio spot de Binance + libro Polymarket
- **Recomendación:** US East óptimo, EU West viable (latencia balanceada)

#### 4. **coin_flip_dog**
- **Umbral crítico:** < 150ms
- **Razón:** Entra T-30 a T-90s; ventana más amplia
- **Recomendación:** Cualquier región < 150ms total

---

### Estrategias poco sensibles

#### 5. **ss_fade** / **ss_trend** (desactivadas)
- **Umbral crítico:** < 300ms
- **Razón:** Entran al inicio de ventana (5 minutos de buffer)
- **Recomendación:** Cualquier región razonable

---

## 🔧 Cómo Verificar Latencia Antes de Contratar

### Script de Test de Latencia

Crea y ejecuta este script **desde tu VPS candidato**:

```bash
# test_latency.sh
#!/bin/bash

echo "🌐 Test de latencia para trading en Polymarket"
echo ""

echo "📍 Polymarket CLOB:"
ping -c 10 clob.polymarket.com | tail -1 | awk '{print $4}' | cut -d '/' -f 2

echo "📍 Polymarket Gamma:"
ping -c 10 gamma-api.polymarket.com | tail -1 | awk '{print $4}' | cut -d '/' -f 2

echo "📍 Binance API:"
ping -c 10 api.binance.com | tail -1 | awk '{print $4}' | cut -d '/' -f 2

echo ""
echo "🧪 Test HTTP (más preciso):"

curl -o /dev/null -s -w "CLOB: %{time_total}s\n" https://clob.polymarket.com
curl -o /dev/null -s -w "Gamma: %{time_total}s\n" https://gamma-api.polymarket.com
curl -o /dev/null -s -w "Binance: %{time_total}s\n" https://api.binance.com/api/v3/ping
```

**Ejecutar:**
```bash
chmod +x test_latency.sh
./test_latency.sh
```

---

### Script Python Detallado

```python
# test_latency_detailed.py
import time
import requests
import statistics

endpoints = {
    "Polymarket CLOB": "https://clob.polymarket.com",
    "Polymarket Gamma": "https://gamma-api.polymarket.com",
    "Binance API": "https://api.binance.com/api/v3/ping",
}

print("🌐 Test de latencia detallado\n")

results = {}
for name, url in endpoints.items():
    latencies = []
    for i in range(20):  # 20 requests para estadística
        try:
            start = time.time()
            response = requests.get(url, timeout=5)
            latency = (time.time() - start) * 1000
            latencies.append(latency)
            time.sleep(0.1)  # No saturar
        except Exception as e:
            print(f"❌ {name}: Error - {e}")
            break
    else:
        avg = statistics.mean(latencies)
        median = statistics.median(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        results[name] = {
            'avg': avg,
            'median': median,
            'p95': p95,
            'min': min(latencies),
            'max': max(latencies)
        }
        
        print(f"{name:25} | Avg: {avg:6.1f}ms | Median: {median:6.1f}ms | P95: {p95:6.1f}ms | Min: {min(latencies):6.1f}ms | Max: {max(latencies):6.1f}ms")

print("\n📊 Evaluación:")
clob_avg = results.get("Polymarket CLOB", {}).get('avg', 999)

if clob_avg < 30:
    print("✅ EXCELENTE - Ideal para todas las estrategias incluyendo near_res")
elif clob_avg < 80:
    print("✅ BUENO - Viable para todas las estrategias, near_res con ventaja")
elif clob_avg < 150:
    print("⚠️ ACEPTABLE - Viable para TA y CFD, near_res en desventaja")
else:
    print("❌ PROBLEMÁTICO - Solo viable para estrategias no time-sensitive")
```

---

## 📋 Checklist para Cambiar Ubicación de VPS

### Antes de cambiar

- [ ] Ejecutar `test_latency.sh` en VPS actual
- [ ] Documentar latencias actuales
- [ ] Verificar que tu proveedor ofrece la región objetivo
- [ ] Comparar precios entre regiones

### Durante el cambio

- [ ] **Backup completo:**
  ```bash
  ssh polymarket@VPS_ACTUAL
  cd ~/Polymarket-BotV1
  sudo systemctl stop polymarket-bot
  
  # Backup DB
  cp data/streak_snapper.db ~/backup_$(date +%Y%m%d).db
  
  # Backup .env
  cp .env ~/backup_env_$(date +%Y%m%d)
  ```

- [ ] Provisionar VPS nuevo en región deseada
- [ ] Ejecutar `test_latency.sh` en VPS nuevo
- [ ] Comparar resultados
- [ ] Si la latencia mejora > 30%, proceder con migración

### Migración

- [ ] Seguir `docs/DEPLOY_VPS_HOSTINGER.md` en VPS nuevo
- [ ] Copiar `.env` del backup:
  ```bash
  scp polymarket@VPS_VIEJO:~/backup_env_* .env
  ```
- [ ] Copiar base de datos (opcional, solo para preservar historial):
  ```bash
  scp polymarket@VPS_VIEJO:~/backup_*.db data/streak_snapper.db
  ```
- [ ] Arrancar bot en VPS nuevo
- [ ] Monitorear primeras 24 horas

### Validación post-migración

- [ ] Latencia mejorada confirmada
- [ ] WebSocket sin desconexiones frecuentes
- [ ] Órdenes ejecutándose más rápido
- [ ] Logs sin errores de timeout
- [ ] Dashboard accesible

### Cleanup

- [ ] Dejar VPS viejo corriendo 24h en paralelo (por si acaso)
- [ ] Si todo OK, cancelar VPS viejo
- [ ] Actualizar DNS si usas dominio

---

## 🎯 Recomendación Final

### Para tu bot actual (Streak Snapper v2)

**Configuración óptima:**

| Estrategia Activa | Región Mínima | Región Óptima |
|-------------------|---------------|---------------|
| temporal_arb | EU West (< 120ms) | **US East (< 30ms)** ⭐ |
| coin_flip_dog | Cualquiera (< 150ms) | **US East (< 30ms)** ⭐ |
| near_res | **Solo US East** | **US East (< 30ms)** ⭐ |
| box_builder | EU West (< 100ms) | **US East (< 30ms)** ⭐ |

**Veredicto:**

### 🏆 **Migrar a US East (Virginia/Nueva York)**

**Razones:**
1. ✅ **near_res** solo es viable con < 50ms
2. ✅ **box_builder** repricing más competitivo
3. ✅ **temporal_arb** detecta oportunidades antes
4. ✅ WebSocket más estable (menos reconnects)
5. ✅ Ventaja competitiva vs traders en otras regiones

**Costo incremental:** +$1-3/mes típicamente (Hostinger cobra igual o similar por región)

**ROI esperado:** Una sola operación adicional ganada por mes por latencia compensa el costo.

---

## 🛠️ Próximos Pasos

### 1. Verificar ubicación actual de tu VPS Hostinger

```bash
ssh polymarket@TU_VPS_IP
curl https://ipapi.co/json/
```

Esto te dirá la región actual.

### 2. Si NO estás en US East, solicitar migración

**Hostinger:**
- Panel de control → VPS → "Rebuild VPS" o "Change Location"
- O contactar soporte para migración sin downtime

**Advertencia:** "Rebuild" borra todo; seguir checklist de backup arriba.

### 3. Alternativa: Provisionar VPS nuevo + migración limpia

Más seguro que rebuild in-place. Ver checklist arriba.

---

## 📞 Contacto y Soporte

**¿Dudas sobre tu región actual?**
Ejecuta en tu VPS:
```bash
curl -s https://ipapi.co/json/ | python3 -m json.tool
```

**¿Quieres comparar proveedores?**
Hostinger, AWS, DigitalOcean, Vultr y Linode todos tienen free trials o créditos iniciales para probar latencia antes de comprometerte.

---

**Última actualización:** 2026-09-04
