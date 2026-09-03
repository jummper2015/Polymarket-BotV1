# 🔌 Conectar al VPS remotamente desde VS Code

Esta guía te permitirá trabajar en el VPS como si fuera tu máquina local.

---

## OPCIÓN 1: VS Code Remote SSH (RECOMENDADO)

### 1. Instalar extensión

1. Abrir VS Code
2. Ir a Extensions (Ctrl+Shift+X)
3. Buscar: **"Remote - SSH"**
4. Instalar la extensión de Microsoft

### 2. Configurar SSH

**En tu máquina local**, editar el archivo SSH config:

```bash
# Windows
notepad %USERPROFILE%\.ssh\config

# Mac/Linux
nano ~/.ssh/config
```

**Añadir esta configuración:**

```
Host polymarket-vps
    HostName TU_IP_VPS_AQUI
    User polymarket
    Port 22
    IdentityFile ~/.ssh/id_rsa
```

**Guardar el archivo**

### 3. Conectar desde VS Code

1. **Abrir paleta de comandos:** `Ctrl+Shift+P` (Windows/Linux) o `Cmd+Shift+P` (Mac)
2. Escribir: **"Remote-SSH: Connect to Host"**
3. Seleccionar: **polymarket-vps**
4. VS Code abrirá una nueva ventana conectada al VPS
5. Cuando pregunte por password, ingresar tu password SSH
6. Una vez conectado, ir a: **File → Open Folder**
7. Seleccionar: `/home/polymarket/Polymarket-BotV1`

**¡Listo!** Ahora puedes editar archivos directamente en el VPS.

### 4. Terminal integrada

Una vez conectado:
- `Ctrl+` (backtick) para abrir terminal
- El terminal ya está en el VPS
- Puedes ejecutar comandos directamente:

```bash
# Ver logs
tail -f logs/bot.log

# Editar .env
nano .env

# Reiniciar bot
sudo systemctl restart polymarket-bot

# Ver estado
sudo systemctl status polymarket-bot
```

---

## OPCIÓN 2: SSH tradicional desde terminal

### Windows (PowerShell o CMD)

```powershell
ssh polymarket@TU_IP_VPS
# Ingresar password cuando lo pida
```

### Mac/Linux

```bash
ssh polymarket@TU_IP_VPS
# Ingresar password cuando lo pida
```

### Una vez conectado

```bash
# Ir al directorio del bot
cd ~/Polymarket-BotV1

# Ver logs en tiempo real
tail -f logs/bot.log

# Editar configuración
nano .env

# Reiniciar bot después de cambios
sudo systemctl restart polymarket-bot

# Desconectar
exit
```

---

## OPCIÓN 3: SSH con clave pública (sin password)

### Generar clave SSH (si no tienes)

**En tu máquina local:**

```bash
# Windows (PowerShell)
ssh-keygen -t rsa -b 4096 -C "tu_email@example.com"

# Mac/Linux
ssh-keygen -t rsa -b 4096 -C "tu_email@example.com"

# Presionar Enter para ubicación por defecto
# Opcionalmente ingresar passphrase (o dejarlo vacío)
```

### Copiar clave al VPS

**Mac/Linux:**
```bash
ssh-copy-id polymarket@TU_IP_VPS
```

**Windows (PowerShell):**
```powershell
type $env:USERPROFILE\.ssh\id_rsa.pub | ssh polymarket@TU_IP_VPS "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

**Ahora puedes conectar sin password:**
```bash
ssh polymarket@TU_IP_VPS
# No pedirá password
```

---

## 🔄 Workflow típico de modificaciones

### Escenario 1: Cambiar configuración

```bash
# 1. Conectar al VPS
ssh polymarket@TU_IP_VPS

# 2. Editar .env
cd ~/Polymarket-BotV1
nano .env
# Hacer cambios y guardar (Ctrl+O, Enter, Ctrl+X)

# 3. Reiniciar bot
sudo systemctl restart polymarket-bot

# 4. Verificar logs
tail -f logs/bot.log
# Ctrl+C para salir
```

### Escenario 2: Modificar código Python

**Opción A: Desde VS Code Remote SSH (recomendado)**
1. Conectar a `polymarket-vps` desde VS Code
2. Editar archivos directamente (ej: `bot/config.py`)
3. Guardar con `Ctrl+S`
4. Abrir terminal integrada (`Ctrl+backtick`)
5. Ejecutar: `sudo systemctl restart polymarket-bot`

**Opción B: Desde terminal SSH**
```bash
ssh polymarket@TU_IP_VPS
cd ~/Polymarket-BotV1
nano bot/config.py  # o el archivo que necesites
# Hacer cambios
sudo systemctl restart polymarket-bot
tail -f logs/bot.log
```

### Escenario 3: Actualizar desde Git

```bash
# Conectar al VPS
ssh polymarket@TU_IP_VPS
cd ~/Polymarket-BotV1

# Detener bot
sudo systemctl stop polymarket-bot

# Backup de .env
cp .env .env.backup

# Pull desde Git
git pull origin main

# Reinstalar dependencias si cambiaron
source venv/bin/activate
pip install -r requirements.txt

# Restaurar .env si fue sobrescrito
cp .env.backup .env

# Reiniciar bot
sudo systemctl start polymarket-bot

# Ver logs
tail -f logs/bot.log
```

### Escenario 4: Ver trades en la base de datos

```bash
ssh polymarket@TU_IP_VPS
cd ~/Polymarket-BotV1

# Entrar a SQLite
sqlite3 data/streak_snapper.db

# Queries útiles:
sqlite> SELECT COUNT(*) FROM trades;
sqlite> SELECT strategy, COUNT(*), SUM(pnl) FROM trades WHERE won IS NOT NULL GROUP BY strategy;
sqlite> SELECT * FROM trades ORDER BY id DESC LIMIT 10;
sqlite> SELECT AVG(pnl), COUNT(*) FROM trades WHERE won=1;
sqlite> .schema trades
sqlite> .exit
```

---

## 📊 Monitoreo continuo

### Ver logs en tiempo real

```bash
ssh polymarket@TU_IP_VPS
tail -f ~/Polymarket-BotV1/logs/bot.log
```

### Dashboard web

Simplemente abrir en el navegador:
```
https://polymarket.tudominio.com
```

### Script de monitoreo automatizado

Si configuraste el script de check (docs/DEPLOY_VPS_HOSTINGER.md Fase 8):

```bash
ssh polymarket@TU_IP_VPS
./check_bot.sh
```

---

## 🔐 Configurar VS Code para reconexión automática

**En VS Code Remote SSH:**

1. Una vez conectado al VPS, ir a: **File → Preferences → Settings**
2. Buscar: `remote.SSH.connectTimeout`
3. Aumentar a: `60` (segundos)
4. Buscar: `remote.SSH.keepAlive`
5. Activar checkbox

Esto mantiene la conexión más estable.

---

## 🆘 Troubleshooting conexión SSH

### "Connection refused"

```bash
# Verificar que el VPS está encendido
ping TU_IP_VPS

# Verificar puerto SSH
telnet TU_IP_VPS 22
```

### "Permission denied"

```bash
# Verificar usuario correcto
ssh polymarket@TU_IP_VPS  # NO root@

# Si cambiastes el puerto SSH:
ssh -p 2222 polymarket@TU_IP_VPS
```

### "Host key verification failed"

```bash
# Remover entrada antigua del known_hosts
ssh-keygen -R TU_IP_VPS

# Volver a conectar
ssh polymarket@TU_IP_VPS
```

### Conexión lenta en VS Code

```bash
# En tu máquina local, editar SSH config
nano ~/.ssh/config

# Añadir estas líneas al host:
Host polymarket-vps
    ...
    Compression yes
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

---

## 📱 SSH desde móvil (opcional)

### Android: Termux

1. Instalar **Termux** desde Play Store
2. Ejecutar:
```bash
pkg install openssh
ssh polymarket@TU_IP_VPS
```

### iOS: Terminus

1. Instalar **Terminus** desde App Store
2. Añadir host:
   - Host: `TU_IP_VPS`
   - User: `polymarket`
   - Port: `22`

---

## ⚡ Comandos rápidos desde local

### Ver logs sin conectar interactivamente

```bash
ssh polymarket@TU_IP_VPS 'tail -20 ~/Polymarket-BotV1/logs/bot.log'
```

### Reiniciar bot remotamente

```bash
ssh polymarket@TU_IP_VPS 'sudo systemctl restart polymarket-bot'
```

### Verificar estado

```bash
ssh polymarket@TU_IP_VPS 'sudo systemctl status polymarket-bot'
```

### Consultar trades totales

```bash
ssh polymarket@TU_IP_VPS 'sqlite3 ~/Polymarket-BotV1/data/streak_snapper.db "SELECT COUNT(*) FROM trades;"'
```

---

## 🎯 Resumen workflow recomendado

**Día a día:**
1. Abrir VS Code
2. Remote SSH → polymarket-vps
3. Editar lo que necesites
4. Terminal integrado para reiniciar bot
5. Abrir dashboard en navegador para verificar

**Modificaciones grandes:**
1. Hacer cambios en repositorio local
2. Commit y push a GitHub
3. SSH al VPS
4. `git pull` para actualizar
5. Reiniciar bot

**Monitoreo:**
- Dashboard web para métricas visuales
- `tail -f logs/bot.log` para logs detallados
- SQLite para análisis profundo de datos

---

**¡Todo listo para trabajar remotamente en tu VPS!**

Las modificaciones que hagas se aplicarán inmediatamente al bot en producción.
