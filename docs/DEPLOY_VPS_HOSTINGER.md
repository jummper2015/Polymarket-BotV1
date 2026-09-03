# 🚀 Guía de Despliegue en VPS Hostinger

## 📋 Requisitos previos

- VPS de Hostinger con Ubuntu 20.04/22.04
- Dominio apuntando al VPS (ej: `polymarket.tudominio.com`)
- Acceso SSH root al VPS
- Git instalado localmente

---

## FASE 1: PREPARACIÓN DEL VPS

### 1.1 Conectar al VPS por SSH

```bash
# Desde tu máquina local
ssh root@TU_IP_VPS
```

### 1.2 Actualizar el sistema

```bash
apt update && apt upgrade -y
```

### 1.3 Instalar dependencias del sistema

```bash
# Python 3.11+ y herramientas esenciales
apt install -y python3 python3-pip python3-venv git curl wget nginx certbot python3-certbot-nginx

# Verificar versión de Python (debe ser 3.9+)
python3 --version
```

### 1.4 Crear usuario para el bot (seguridad)

```bash
# No correr el bot como root
adduser --disabled-password --gecos "" polymarket
usermod -aG sudo polymarket

# Cambiar a ese usuario
su - polymarket
```

---

## FASE 2: CLONAR Y CONFIGURAR EL PROYECTO

### 2.1 Clonar el repositorio

```bash
cd ~
git clone https://github.com/TU_USUARIO/Polymarket-BotV1.git
cd Polymarket-BotV1
```

### 2.2 Crear entorno virtual

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2.3 Instalar dependencias Python

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.4 Configurar variables de entorno

```bash
# Copiar el ejemplo
cp .env.example .env

# Editar con nano o vim
nano .env
```

**Configuración recomendada para PAPER mode en VPS:**

```bash
# ============================================================
# MODO DE OPERACIÓN
# ============================================================
TRADING_MODE=paper

# ============================================================
# ESTRATEGIA
# ============================================================
STARTING_BANKROLL=1000.0
SS_ENABLED=true
SS_SYMBOLS=btc
SS_SIZING=flat

# Activar estrategias para testing
CFD_ENABLED=true
TA_ENABLED=true
NRC_ENABLED=false
BB_ENABLED=false

# ============================================================
# DASHBOARD
# ============================================================
PORT=5000
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PASSWORD=TU_PASSWORD_SEGURO_AQUI

# Generar secret key:
# python3 -c "import secrets; print(secrets.token_hex(32))"
DASHBOARD_SECRET_KEY=PEGAR_AQUI_EL_RESULTADO

# ============================================================
# POLYMARKET API
# ============================================================
CLOB_HOST=https://clob.polymarket.com
GAMMA_HOST=https://gamma-api.polymarket.com
CLOB_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
CHAIN_ID=137
SIGNATURE_TYPE=2
```

**Guardar**: `Ctrl+O`, `Enter`, `Ctrl+X`

### 2.5 Generar el secret key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Copiar el resultado y pegarlo en .env como DASHBOARD_SECRET_KEY
```

### 2.6 Crear directorio de datos

```bash
mkdir -p data
chmod 755 data
```

---

## FASE 3: CONFIGURAR SYSTEMD (auto-inicio del bot)

### 3.1 Crear archivo de servicio

```bash
# Volver a root temporalmente
exit  # Salir del usuario polymarket
```

```bash
# Como root, crear el servicio
nano /etc/systemd/system/polymarket-bot.service
```

**Contenido del archivo:**

```ini
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=polymarket
WorkingDirectory=/home/polymarket/Polymarket-BotV1
Environment="PATH=/home/polymarket/Polymarket-BotV1/venv/bin"
ExecStart=/home/polymarket/Polymarket-BotV1/venv/bin/python run.py
Restart=always
RestartSec=10
StandardOutput=append:/home/polymarket/Polymarket-BotV1/logs/bot.log
StandardError=append:/home/polymarket/Polymarket-BotV1/logs/bot.log

[Install]
WantedBy=multi-user.target
```

**Guardar**: `Ctrl+O`, `Enter`, `Ctrl+X`

### 3.2 Crear directorio de logs

```bash
mkdir -p /home/polymarket/Polymarket-BotV1/logs
chown -R polymarket:polymarket /home/polymarket/Polymarket-BotV1/logs
```

### 3.3 Activar y arrancar el servicio

```bash
# Recargar systemd
systemctl daemon-reload

# Habilitar para auto-inicio al reboot
systemctl enable polymarket-bot

# Arrancar el bot
systemctl start polymarket-bot

# Verificar estado
systemctl status polymarket-bot
```

### 3.4 Ver logs en tiempo real

```bash
# Logs del servicio
journalctl -u polymarket-bot -f

# O ver el archivo de log
tail -f /home/polymarket/Polymarket-BotV1/logs/bot.log
```

---

## FASE 4: CONFIGURAR NGINX (reverse proxy + dominio)

### 4.1 Configurar Nginx

```bash
# Como root
nano /etc/nginx/sites-available/polymarket
```

**Contenido del archivo:**

```nginx
server {
    listen 80;
    server_name polymarket.tudominio.com;  # CAMBIAR POR TU DOMINIO

    # Redirigir todo a HTTPS (después de configurar SSL)
    # return 301 https://$server_name$request_uri;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # WebSocket support
        proxy_read_timeout 86400;
    }
}
```

**Guardar**: `Ctrl+O`, `Enter`, `Ctrl+X`

### 4.2 Activar el sitio

```bash
# Crear symlink
ln -s /etc/nginx/sites-available/polymarket /etc/nginx/sites-enabled/

# Probar configuración
nginx -t

# Si todo OK, recargar nginx
systemctl reload nginx
```

---

## FASE 5: CONFIGURAR SSL (HTTPS con Let's Encrypt)

### 5.1 Obtener certificado SSL

```bash
# Asegurarse de que el dominio apunta al VPS antes de ejecutar esto
certbot --nginx -d polymarket.tudominio.com
```

**Responder las preguntas:**
- Email: tu email
- Aceptar términos: Y
- ¿Redirigir HTTP a HTTPS?: 2 (sí)

### 5.2 Auto-renovación del certificado

```bash
# Verificar que el timer está activo
systemctl status certbot.timer

# Probar renovación (dry-run)
certbot renew --dry-run
```

---

## FASE 6: VERIFICAR QUE TODO FUNCIONA

### 6.1 Verificar el bot

```bash
# Estado del servicio
systemctl status polymarket-bot

# Ver logs
tail -f /home/polymarket/Polymarket-BotV1/logs/bot.log
```

### 6.2 Acceder al dashboard

```
https://polymarket.tudominio.com
```

**Login:**
- Usuario: (no hay usuario, solo password)
- Password: el que pusiste en `DASHBOARD_PASSWORD`

### 6.3 Comandos útiles

```bash
# Reiniciar el bot
systemctl restart polymarket-bot

# Detener el bot
systemctl stop polymarket-bot

# Ver estado
systemctl status polymarket-bot

# Ver logs en vivo
journalctl -u polymarket-bot -f
```

---

## FASE 7: COMANDOS SSH PARA MODIFICACIONES FUTURAS

### 7.1 Conectar al VPS

```bash
# Desde tu máquina local
ssh polymarket@TU_IP_VPS
```

### 7.2 Editar archivos del bot

```bash
cd ~/Polymarket-BotV1

# Editar configuración
nano .env

# Editar código
nano bot/config.py

# Después de modificar, reiniciar el bot
sudo systemctl restart polymarket-bot
```

### 7.3 Actualizar código desde Git

```bash
cd ~/Polymarket-BotV1

# Detener el bot
sudo systemctl stop polymarket-bot

# Hacer backup de .env
cp .env .env.backup

# Actualizar código
git pull origin main

# Reinstalar dependencias si hay cambios
source venv/bin/activate
pip install -r requirements.txt

# Restaurar .env si git lo sobrescribió
cp .env.backup .env

# Reiniciar el bot
sudo systemctl start polymarket-bot
```

### 7.4 Ver base de datos

```bash
cd ~/Polymarket-BotV1

# Entrar a la DB con sqlite3
sqlite3 data/streak_snapper.db

# Queries útiles
sqlite> SELECT COUNT(*) FROM trades;
sqlite> SELECT strategy, COUNT(*), SUM(pnl) FROM trades WHERE status='won' OR status='lost' GROUP BY strategy;
sqlite> SELECT * FROM trades ORDER BY id DESC LIMIT 10;
sqlite> .exit
```

---

## FASE 8: MONITOREO Y MANTENIMIENTO

### 8.1 Script de monitoreo (opcional)

```bash
nano ~/check_bot.sh
```

**Contenido:**

```bash
#!/bin/bash

# Verificar si el bot está corriendo
if ! systemctl is-active --quiet polymarket-bot; then
    echo "⚠️ Bot no está corriendo. Reiniciando..."
    systemctl start polymarket-bot
    echo "✅ Bot reiniciado"
else
    echo "✅ Bot corriendo OK"
fi

# Verificar si el dashboard responde
response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/healthz)
if [ "$response" != "200" ]; then
    echo "⚠️ Dashboard no responde. Reiniciando..."
    systemctl restart polymarket-bot
fi

# Mostrar últimas 5 líneas del log
echo "📊 Últimos logs:"
tail -5 ~/Polymarket-BotV1/logs/bot.log
```

```bash
# Hacer ejecutable
chmod +x ~/check_bot.sh

# Ejecutar manualmente
./check_bot.sh

# Añadir a cron para ejecutar cada hora
crontab -e
# Añadir esta línea:
# 0 * * * * /home/polymarket/check_bot.sh >> /home/polymarket/monitor.log 2>&1
```

### 8.2 Backup automático de la base de datos

```bash
nano ~/backup_db.sh
```

**Contenido:**

```bash
#!/bin/bash

BACKUP_DIR="/home/polymarket/backups"
mkdir -p $BACKUP_DIR

# Backup con timestamp
DATE=$(date +%Y%m%d_%H%M%S)
cp ~/Polymarket-BotV1/data/streak_snapper.db "$BACKUP_DIR/streak_snapper_$DATE.db"

# Mantener solo los últimos 7 backups
cd $BACKUP_DIR
ls -t streak_snapper_*.db | tail -n +8 | xargs -r rm

echo "✅ Backup creado: streak_snapper_$DATE.db"
```

```bash
# Hacer ejecutable
chmod +x ~/backup_db.sh

# Añadir a cron para backup diario a las 2 AM
crontab -e
# Añadir:
# 0 2 * * * /home/polymarket/backup_db.sh >> /home/polymarket/backup.log 2>&1
```

---

## 🔥 COMANDOS RÁPIDOS DE REFERENCIA

```bash
# ===== SERVICIO =====
sudo systemctl start polymarket-bot      # Arrancar
sudo systemctl stop polymarket-bot       # Detener
sudo systemctl restart polymarket-bot    # Reiniciar
sudo systemctl status polymarket-bot     # Estado

# ===== LOGS =====
tail -f ~/Polymarket-BotV1/logs/bot.log         # Ver logs
journalctl -u polymarket-bot -f                 # Logs systemd
journalctl -u polymarket-bot --since "1 hour ago"  # Última hora

# ===== BASE DE DATOS =====
sqlite3 ~/Polymarket-BotV1/data/streak_snapper.db "SELECT COUNT(*) FROM trades;"
sqlite3 ~/Polymarket-BotV1/data/streak_snapper.db "SELECT * FROM trades ORDER BY id DESC LIMIT 5;"

# ===== NGINX =====
sudo systemctl reload nginx              # Recargar config
sudo nginx -t                            # Probar config
sudo tail -f /var/log/nginx/error.log   # Ver errores

# ===== CERTIFICADO SSL =====
sudo certbot certificates                # Ver certificados
sudo certbot renew                       # Renovar manualmente

# ===== RECURSOS DEL SISTEMA =====
htop                                     # Monitor de procesos
df -h                                    # Espacio en disco
free -h                                  # Memoria RAM
```

---

## ⚠️ TROUBLESHOOTING COMÚN

### Bot no arranca

```bash
# Ver el error específico
journalctl -u polymarket-bot -n 50

# Verificar permisos
ls -la ~/Polymarket-BotV1/data/

# Verificar que el venv existe
ls -la ~/Polymarket-BotV1/venv/

# Reinstalar dependencias
cd ~/Polymarket-BotV1
source venv/bin/activate
pip install -r requirements.txt
```

### Dashboard no carga

```bash
# Verificar que el bot está escuchando en el puerto
sudo netstat -tulpn | grep 5000

# Verificar nginx
sudo nginx -t
sudo systemctl status nginx

# Ver logs de nginx
sudo tail -f /var/log/nginx/error.log
```

### WebSocket se desconecta

```bash
# Aumentar timeouts en nginx
sudo nano /etc/nginx/sites-available/polymarket

# Añadir en location /:
proxy_read_timeout 86400;
proxy_connect_timeout 600;
proxy_send_timeout 600;

# Recargar nginx
sudo systemctl reload nginx
```

### Base de datos bloqueada

```bash
# Verificar procesos usando la DB
lsof ~/Polymarket-BotV1/data/streak_snapper.db

# Si hay procesos zombies, matar y reiniciar
sudo systemctl restart polymarket-bot
```

---

## 🎯 SIGUIENTES PASOS

1. **Dejar correr 2-4 semanas en paper mode**
   - Monitorear el dashboard diariamente
   - Objetivo: 200+ trades con P&L positivo

2. **Revisar estrategias activas**
   - Temporal Arb ya tiene 160 trades
   - Coin Flip Dog necesita más datos
   - Near Res validar con cautela (riesgo de cola)

3. **Antes de ir a REAL:**
   - Migrar a PostgreSQL (ver docs/DEPLOY_VPS_HOSTINGER.md - Fase 9)
   - Implementar alertas por Telegram
   - Configurar límites de riesgo

---

## 📞 CONEXIÓN REMOTA SSH DESDE VS CODE

### Opción 1: VS Code Remote SSH

```bash
# 1. Instalar extensión "Remote - SSH" en VS Code

# 2. Abrir paleta de comandos (Ctrl+Shift+P)
# Buscar: "Remote-SSH: Connect to Host"

# 3. Añadir host:
polymarket@TU_IP_VPS

# 4. Una vez conectado, abrir carpeta:
/home/polymarket/Polymarket-BotV1
```

### Opción 2: SSH Config para acceso rápido

```bash
# En tu máquina local
nano ~/.ssh/config
```

**Añadir:**

```
Host polymarket-vps
    HostName TU_IP_VPS
    User polymarket
    Port 22
    IdentityFile ~/.ssh/id_rsa
```

**Conectar:**

```bash
# Desde terminal
ssh polymarket-vps

# Desde VS Code
# Remote-SSH: Connect to Host → polymarket-vps
```

---

## ✅ CHECKLIST DE DESPLIEGUE

```markdown
- [ ] VPS configurado y actualizado
- [ ] Usuario polymarket creado
- [ ] Repositorio clonado
- [ ] Entorno virtual creado
- [ ] Dependencias instaladas
- [ ] .env configurado con password seguro
- [ ] DASHBOARD_SECRET_KEY generado
- [ ] Servicio systemd creado
- [ ] Bot arrancado y corriendo
- [ ] Logs verificados (sin errores)
- [ ] Nginx configurado
- [ ] Dominio apuntando al VPS
- [ ] SSL configurado con Let's Encrypt
- [ ] Dashboard accesible por HTTPS
- [ ] Login funciona con la password
- [ ] Trades apareciendo en el dashboard
- [ ] Script de monitoreo configurado
- [ ] Backup automático configurado
- [ ] SSH config para acceso rápido
```

---

## 🚨 SEGURIDAD

1. **Cambiar puerto SSH** (opcional pero recomendado):
```bash
sudo nano /etc/ssh/sshd_config
# Cambiar Port 22 a Port 2222
sudo systemctl restart sshd
```

2. **Configurar firewall**:
```bash
sudo ufw allow 2222/tcp  # SSH (o 22 si no cambiaste el puerto)
sudo ufw allow 80/tcp    # HTTP
sudo ufw allow 443/tcp   # HTTPS
sudo ufw enable
sudo ufw status
```

3. **Deshabilitar login root por SSH**:
```bash
sudo nano /etc/ssh/sshd_config
# PermitRootLogin no
sudo systemctl restart sshd
```

---

**¡Listo! Tu bot está corriendo 24/7 en tu VPS de Hostinger en modo paper.**

Para cualquier modificación futura, simplemente conéctate por SSH y edita los archivos.
Todos los cambios requieren `sudo systemctl restart polymarket-bot` para aplicarse.
