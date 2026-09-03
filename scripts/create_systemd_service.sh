#!/bin/bash

# ============================================================
# Script para crear el servicio systemd
# Ejecutar como ROOT
# ============================================================

if [ "$EUID" -ne 0 ]; then
    echo "❌ Este script debe ejecutarse como root"
    echo "   Usar: sudo bash scripts/create_systemd_service.sh"
    exit 1
fi

# Colores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "🔧 Creando servicio systemd para Polymarket Bot..."
echo ""

# Detectar el usuario y directorio
POLYMARKET_USER=${1:-polymarket}
POLYMARKET_DIR="/home/$POLYMARKET_USER/Polymarket-BotV1"

if [ ! -d "$POLYMARKET_DIR" ]; then
    echo "❌ No se encuentra el directorio: $POLYMARKET_DIR"
    echo "   Uso: sudo bash scripts/create_systemd_service.sh [usuario]"
    exit 1
fi

echo "📁 Directorio: $POLYMARKET_DIR"
echo "👤 Usuario: $POLYMARKET_USER"
echo ""

# Crear archivo de servicio
SERVICE_FILE="/etc/systemd/system/polymarket-bot.service"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=$POLYMARKET_USER
WorkingDirectory=$POLYMARKET_DIR
Environment="PATH=$POLYMARKET_DIR/venv/bin"
ExecStart=$POLYMARKET_DIR/venv/bin/python run.py
Restart=always
RestartSec=10
StandardOutput=append:$POLYMARKET_DIR/logs/bot.log
StandardError=append:$POLYMARKET_DIR/logs/bot.log

# Límites de recursos (opcional)
# MemoryMax=512M
# CPUQuota=50%

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}✅ Archivo de servicio creado: $SERVICE_FILE${NC}"
echo ""

# Crear directorio de logs si no existe
mkdir -p "$POLYMARKET_DIR/logs"
chown -R "$POLYMARKET_USER:$POLYMARKET_USER" "$POLYMARKET_DIR/logs"

# Recargar systemd
systemctl daemon-reload
echo -e "${GREEN}✅ Systemd recargado${NC}"

# Habilitar el servicio
systemctl enable polymarket-bot
echo -e "${GREEN}✅ Servicio habilitado para auto-inicio${NC}"

echo ""
echo "════════════════════════════════════════════════════════"
echo -e "${GREEN}✅ Servicio systemd configurado correctamente${NC}"
echo "════════════════════════════════════════════════════════"
echo ""
echo "📝 Comandos útiles:"
echo ""
echo "   Arrancar:    systemctl start polymarket-bot"
echo "   Detener:     systemctl stop polymarket-bot"
echo "   Reiniciar:   systemctl restart polymarket-bot"
echo "   Estado:      systemctl status polymarket-bot"
echo "   Logs:        journalctl -u polymarket-bot -f"
echo ""
echo -e "${YELLOW}⚠️  Recuerda configurar DASHBOARD_PASSWORD en .env antes de arrancar${NC}"
echo ""
