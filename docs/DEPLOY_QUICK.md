# 🚀 Despliegue Rápido en VPS Hostinger

Esta es la guía resumida. Para detalles completos ver: **[docs/DEPLOY_VPS_HOSTINGER.md](docs/DEPLOY_VPS_HOSTINGER.md)**

---

## 📋 Pre-requisitos

- VPS con Ubuntu 20.04/22.04
- Dominio apuntando al VPS
- Acceso SSH root

---

## ⚡ Instalación en 5 pasos

### 1. Conectar al VPS y preparar usuario

```bash
# Conectar como root
ssh root@TU_IP_VPS

# Actualizar sistema
apt update && apt upgrade -y

# Instalar dependencias base
apt install -y python3 python3-pip python3-venv git curl nginx certbot python3-certbot-nginx

# Crear usuario
adduser --disabled-password --gecos "" polymarket
su - polymarket
```

### 2. Clonar repositorio e instalar

```bash
# Como usuario polymarket
cd ~
git clone https://github.com/TU_USUARIO/Polymarket-BotV1.git
cd Polymarket-BotV1

# Ejecutar script de instalación
bash scripts/vps_setup.sh
```

### 3. Configurar .env

```bash
nano .env
```

**Cambiar obligatoriamente:**
- `DASHBOARD_PASSWORD=TuPasswordSeguro`
- `DASHBOARD_SECRET_KEY` ya está generado por el script

**Guardar:** `Ctrl+O`, `Enter`, `Ctrl+X`

### 4. Crear servicio systemd

```bash
# Salir del usuario polymarket
exit

# Como root, crear el servicio
sudo bash /home/polymarket/Polymarket-BotV1/scripts/create_systemd_service.sh

# Arrancar el bot
sudo systemctl start polymarket-bot

# Verificar que está corriendo
sudo systemctl status polymarket-bot

# Ver logs
tail -f /home/polymarket/Polymarket-BotV1/logs/bot.log
```

### 5. Configurar Nginx + SSL

```bash
# Como root
sudo bash /home/polymarket/Polymarket-BotV1/scripts/setup_nginx.sh polymarket.tudominio.com

# Seguir las instrucciones del script para SSL
```

---

## ✅ Verificación

1. **Bot corriendo:**
   ```bash
   sudo systemctl status polymarket-bot
   ```

2. **Acceder al dashboard:**
   ```
   https://polymarket.tudominio.com
   ```

3. **Ver trades en tiempo real:**
   - Login con el password de `.env`
   - Verificar que aparecen trades

---

## 🔧 Comandos útiles

```bash
# SERVICIO
sudo systemctl start polymarket-bot      # Arrancar
sudo systemctl stop polymarket-bot       # Detener
sudo systemctl restart polymarket-bot    # Reiniciar
sudo systemctl status polymarket-bot     # Estado

# LOGS
tail -f /home/polymarket/Polymarket-BotV1/logs/bot.log
journalctl -u polymarket-bot -f

# EDITAR CONFIG
nano /home/polymarket/Polymarket-BotV1/.env
sudo systemctl restart polymarket-bot  # Aplicar cambios

# BASE DE DATOS
cd /home/polymarket/Polymarket-BotV1
sqlite3 data/streak_snapper.db
sqlite> SELECT COUNT(*) FROM trades;
sqlite> .exit
```

---

## 🔄 Actualizar código desde Git

```bash
# Conectar al VPS
ssh polymarket@TU_IP_VPS

# Ir al directorio
cd ~/Polymarket-BotV1

# Detener bot
sudo systemctl stop polymarket-bot

# Backup de .env
cp .env .env.backup

# Actualizar
git pull origin main

# Reinstalar dependencias si cambiaron
source venv/bin/activate
pip install -r requirements.txt

# Reiniciar bot
sudo systemctl start polymarket-bot
```

---

## 🔐 Conexión SSH desde VS Code

1. Instalar extensión **"Remote - SSH"**
2. `Ctrl+Shift+P` → "Remote-SSH: Connect to Host"
3. Ingresar: `polymarket@TU_IP_VPS`
4. Abrir carpeta: `/home/polymarket/Polymarket-BotV1`

Ahora puedes editar los archivos directamente desde VS Code como si estuvieran en tu máquina local.

---

## 📊 Monitoreo

### Dashboard web
```
https://polymarket.tudominio.com
```

### Logs en tiempo real
```bash
ssh polymarket@TU_IP_VPS
tail -f ~/Polymarket-BotV1/logs/bot.log
```

### Health check
```bash
curl https://polymarket.tudominio.com/healthz
```

---

## ⚠️ Troubleshooting

### Bot no arranca
```bash
# Ver error específico
journalctl -u polymarket-bot -n 50

# Verificar permisos
ls -la ~/Polymarket-BotV1/data/

# Reinstalar dependencias
cd ~/Polymarket-BotV1
source venv/bin/activate
pip install -r requirements.txt
```

### Dashboard no carga
```bash
# Verificar puerto
sudo netstat -tulpn | grep 5000

# Ver logs nginx
sudo tail -f /var/log/nginx/polymarket_error.log

# Probar config nginx
sudo nginx -t
```

### SSL no funciona
```bash
# Verificar certificados
sudo certbot certificates

# Renovar manualmente
sudo certbot renew

# Verificar que el dominio apunta al VPS
dig polymarket.tudominio.com
```

---

## 📖 Documentación completa

- **[docs/DEPLOY_VPS_HOSTINGER.md](docs/DEPLOY_VPS_HOSTINGER.md)** - Guía detallada paso a paso
- **[CLAUDE.md](CLAUDE.md)** - Arquitectura del bot
- **[README.md](README.md)** - Documentación general

---

## 🎯 Próximos pasos

1. ✅ Bot corriendo en paper mode
2. ⏳ Dejar correr 2-4 semanas
3. ⏳ Acumular 200+ trades
4. ⏳ Validar P&L positivo
5. ⏳ Migrar a PostgreSQL
6. ⏳ Implementar alertas
7. ⏳ Ir a modo real

---

**¿Problemas?** Revisa la guía completa en `docs/DEPLOY_VPS_HOSTINGER.md`
