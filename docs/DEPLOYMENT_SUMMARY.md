# 📦 GUÍA COMPLETA DE DESPLIEGUE - RESUMEN EJECUTIVO

**Polymarket Bot - De desarrollo a VPS en producción**

---

## 🎯 Lo que hemos preparado

Tienes todo listo para desplegar el bot en tu VPS de Hostinger en modo paper. Aquí está el resumen completo:

---

## 📚 DOCUMENTACIÓN CREADA

| Documento | Propósito | Cuándo usarlo |
|-----------|-----------|---------------|
| **DEPLOY_QUICK.md** | Guía rápida de 5 pasos | Primera lectura, resumen ejecutivo |
| **docs/DEPLOY_VPS_HOSTINGER.md** | Guía detallada completa | Despliegue paso a paso, troubleshooting |
| **docs/REMOTE_SSH_GUIDE.md** | Conexión remota y workflow | Después del despliegue, trabajo diario |

---

## 🛠️ SCRIPTS AUTOMATIZADOS CREADOS

Todos en `scripts/`:

| Script | Ejecutar como | Qué hace |
|--------|--------------|----------|
| **vps_setup.sh** | `polymarket` | Instala el bot (venv, dependencias, .env) |
| **create_systemd_service.sh** | `root` | Crea servicio para auto-inicio |
| **setup_nginx.sh** | `root` | Configura Nginx + SSL |
| **validate_deployment.sh** | `polymarket` | Valida que todo está correcto |

---

## 🚀 PROCESO DE DESPLIEGUE (5 PASOS)

### 1️⃣ Preparar VPS
```bash
ssh root@TU_IP_VPS
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx
adduser --disabled-password --gecos "" polymarket
su - polymarket
```

### 2️⃣ Instalar Bot
```bash
cd ~
git clone https://github.com/TU_USUARIO/Polymarket-BotV1.git
cd Polymarket-BotV1
bash scripts/vps_setup.sh
```

### 3️⃣ Configurar
```bash
nano .env
# Cambiar DASHBOARD_PASSWORD
```

### 4️⃣ Crear Servicio
```bash
exit  # Salir de usuario polymarket
sudo bash /home/polymarket/Polymarket-BotV1/scripts/create_systemd_service.sh
sudo systemctl start polymarket-bot
```

### 5️⃣ Configurar Web
```bash
sudo bash /home/polymarket/Polymarket-BotV1/scripts/setup_nginx.sh tudominio.com
```

---

## ✅ VALIDACIÓN

```bash
# Como usuario polymarket
cd ~/Polymarket-BotV1
bash scripts/validate_deployment.sh
```

Este script verifica:
- ✅ Proyecto instalado correctamente
- ✅ Dependencias instaladas
- ✅ .env configurado
- ✅ Servicio systemd corriendo
- ✅ Nginx y SSL configurados
- ✅ Bot respondiendo
- ✅ Base de datos funcionando

---

## 🔌 CONEXIÓN REMOTA

### VS Code (Recomendado)
1. Instalar extensión "Remote - SSH"
2. Ctrl+Shift+P → "Remote-SSH: Connect to Host"
3. Ingresar: `polymarket@TU_IP_VPS`
4. Abrir carpeta: `/home/polymarket/Polymarket-BotV1`

### Terminal tradicional
```bash
ssh polymarket@TU_IP_VPS
cd ~/Polymarket-BotV1
```

**Ver guía completa:** `docs/REMOTE_SSH_GUIDE.md`

---

## 🎛️ COMANDOS ESENCIALES

### Control del bot
```bash
sudo systemctl start polymarket-bot      # Arrancar
sudo systemctl stop polymarket-bot       # Detener
sudo systemctl restart polymarket-bot    # Reiniciar
sudo systemctl status polymarket-bot     # Estado
```

### Logs
```bash
tail -f ~/Polymarket-BotV1/logs/bot.log           # Ver logs
journalctl -u polymarket-bot -f                   # Logs systemd
journalctl -u polymarket-bot --since "1 hour ago" # Última hora
```

### Base de datos
```bash
sqlite3 ~/Polymarket-BotV1/data/streak_snapper.db
sqlite> SELECT COUNT(*) FROM trades;
sqlite> SELECT strategy, COUNT(*), SUM(pnl) FROM trades WHERE won IS NOT NULL GROUP BY strategy;
sqlite> .exit
```

### Actualizar código
```bash
cd ~/Polymarket-BotV1
sudo systemctl stop polymarket-bot
cp .env .env.backup
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start polymarket-bot
```

---

## 📊 MONITOREO

### Dashboard web
```
https://tudominio.com
```
Login con: `DASHBOARD_PASSWORD` del .env

### Logs en tiempo real
```bash
ssh polymarket@TU_IP_VPS
tail -f ~/Polymarket-BotV1/logs/bot.log
```

### Health check
```bash
curl https://tudominio.com/healthz
```

---

## 🔧 CONFIGURACIÓN RECOMENDADA

### .env para PAPER mode en VPS

```bash
TRADING_MODE=paper
STARTING_BANKROLL=1000.0

# Estrategias activas para testing
SS_ENABLED=true
SS_SYMBOLS=btc
CFD_ENABLED=true    # Coin Flip Dog
TA_ENABLED=true     # Temporal Arb
NRC_ENABLED=false   # Near Res (riesgo alto)
BB_ENABLED=false    # Box Builder (necesita credenciales maker)

# Sizing conservador
SS_SIZING=flat

# Dashboard
PORT=5000
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PASSWORD=TuPasswordSeguro123
DASHBOARD_SECRET_KEY=<generar con python3 -c "import secrets; print(secrets.token_hex(32))">
```

---

## 📈 ROADMAP DE TESTING

### Fase 1: Paper Testing (2-4 semanas)
- ✅ Bot instalado en VPS
- ⏳ Dejar correr 24/7
- ⏳ Objetivo: 200+ trades
- ⏳ Monitorear P&L diario
- ⏳ Validar win rate ≥ 50%

### Fase 2: Validación (1 semana)
- ⏳ Analizar métricas
- ⏳ Ajustar parámetros si necesario
- ⏳ Verificar estabilidad (uptime ≥ 99%)

### Fase 3: Pre-producción (antes de real)
- ⏳ Migrar a PostgreSQL
- ⏳ Implementar alertas (Telegram)
- ⏳ Configurar límites de riesgo
- ⏳ Test con $100 reales

### Fase 4: Real
- ⏳ Bankroll completo
- ⏳ Monitoreo 24/7
- ⏳ Review semanal

---

## 🆘 SOPORTE RÁPIDO

### Bot no arranca
```bash
journalctl -u polymarket-bot -n 50
# Ver el error específico
```

### Dashboard no carga
```bash
sudo netstat -tulpn | grep 5000
sudo tail -f /var/log/nginx/polymarket_error.log
```

### WebSocket se desconecta
Ver logs del bot:
```bash
tail -f ~/Polymarket-BotV1/logs/bot.log | grep "feed"
```

### SSL no funciona
```bash
sudo certbot certificates
sudo certbot renew --dry-run
```

---

## 📁 ESTRUCTURA DEL PROYECTO EN VPS

```
/home/polymarket/Polymarket-BotV1/
├── .env                    # Tu configuración
├── run.py                  # Entry point
├── bot/                    # Código del bot
├── data/
│   └── streak_snapper.db   # SQLite database
├── logs/
│   └── bot.log            # Logs del bot
├── venv/                   # Python virtual env
├── scripts/               # Scripts de despliegue
├── docs/                  # Documentación
└── requirements.txt       # Dependencias Python
```

---

## 🔐 SEGURIDAD

### Configurar firewall
```bash
sudo ufw allow 22/tcp    # SSH
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable
```

### Cambiar puerto SSH (opcional)
```bash
sudo nano /etc/ssh/sshd_config
# Port 2222
sudo systemctl restart sshd
sudo ufw allow 2222/tcp
```

### Deshabilitar login root
```bash
sudo nano /etc/ssh/sshd_config
# PermitRootLogin no
sudo systemctl restart sshd
```

---

## 📞 CONTACTO Y PRÓXIMOS PASOS

### Inmediato (hoy)
1. ✅ Conectar al VPS
2. ✅ Ejecutar scripts de instalación
3. ✅ Validar que el bot corre
4. ✅ Acceder al dashboard

### Esta semana
1. Dejar correr 24/7
2. Monitorear dashboard diariamente
3. Revisar logs si hay errores
4. Familiarizarse con SSH remoto

### Próximas semanas
1. Acumular 200+ trades en paper
2. Analizar estrategias
3. Ajustar parámetros según resultados

### Antes de ir a REAL
1. Migrar a PostgreSQL
2. Implementar alertas
3. Configurar límites de riesgo
4. Hacer test con capital mínimo

---

## 🎓 RECURSOS

| Recurso | Ubicación | Propósito |
|---------|-----------|-----------|
| Guía rápida | `DEPLOY_QUICK.md` | Resumen de 5 pasos |
| Guía completa | `docs/DEPLOY_VPS_HOSTINGER.md` | Paso a paso detallado |
| SSH remoto | `docs/REMOTE_SSH_GUIDE.md` | Trabajo remoto diario |
| Arquitectura | `CLAUDE.md` | Cómo funciona el bot |
| Roadmap | `docs/RUTA.md` | Estrategias y métricas |
| Archivos | `docs/ARCHIVOS.md` | Estructura del código |

---

## ✅ CHECKLIST FINAL

```markdown
INSTALACIÓN
- [ ] VPS de Hostinger listo
- [ ] Dominio apuntando al VPS
- [ ] Python 3.9+ instalado
- [ ] Git instalado
- [ ] Usuario polymarket creado
- [ ] Repositorio clonado
- [ ] Script vps_setup.sh ejecutado
- [ ] .env configurado con password seguro
- [ ] DASHBOARD_SECRET_KEY generado

SERVICIO
- [ ] Servicio systemd creado
- [ ] Bot arrancado
- [ ] Auto-inicio habilitado
- [ ] Logs verificados sin errores

WEB
- [ ] Nginx instalado y corriendo
- [ ] Configuración de polymarket creada
- [ ] Dominio accesible
- [ ] SSL configurado
- [ ] Dashboard accesible por HTTPS

VALIDACIÓN
- [ ] Script validate_deployment.sh ejecutado
- [ ] Todos los checks pasaron
- [ ] Dashboard muestra métricas
- [ ] Trades apareciendo en tiempo real

ACCESO REMOTO
- [ ] VS Code Remote SSH configurado
- [ ] Conexión SSH funciona sin problemas
- [ ] Terminal integrado funciona
- [ ] Puedes editar archivos remotamente

MONITOREO
- [ ] Dashboard web accesible desde cualquier lugar
- [ ] Logs se pueden ver en tiempo real
- [ ] Base de datos consultable
- [ ] Health check responde OK
```

---

## 🎉 ¡FELICIDADES!

Si todos los checks pasaron, tu bot está:
- ✅ Corriendo 24/7 en tu VPS
- ✅ Operando en modo paper (sin riesgo)
- ✅ Accesible desde cualquier lugar
- ✅ Monitoreado por dashboard web
- ✅ Listo para testing exhaustivo

**Próximo paso:** Dejar correr 2-4 semanas y acumular datos para validar las estrategias.

---

**¿Dudas?** Revisa las guías completas en `docs/`

**¿Problemas?** Ejecuta `bash scripts/validate_deployment.sh` para diagnóstico automático
