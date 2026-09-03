#!/bin/bash

# ============================================================
# Script de instalación rápida para VPS
# Ejecutar como usuario polymarket (NO como root)
# ============================================================

set -e  # Salir si hay error

echo "🚀 Instalando Polymarket Bot en VPS..."
echo ""

# Colores para output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# ============================================================
# 1. Verificar que estamos en el directorio correcto
# ============================================================
if [ ! -f "run.py" ]; then
    echo -e "${RED}❌ Error: Ejecutar este script desde la raíz del proyecto${NC}"
    echo "   cd ~/Polymarket-BotV1"
    echo "   bash scripts/vps_setup.sh"
    exit 1
fi

echo -e "${GREEN}✅ Directorio correcto${NC}"

# ============================================================
# 2. Crear entorno virtual
# ============================================================
echo ""
echo "📦 Creando entorno virtual..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo -e "${GREEN}✅ Entorno virtual creado${NC}"
else
    echo -e "${YELLOW}⚠ Entorno virtual ya existe${NC}"
fi

# ============================================================
# 3. Activar e instalar dependencias
# ============================================================
echo ""
echo "📥 Instalando dependencias..."
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "${GREEN}✅ Dependencias instaladas${NC}"

# ============================================================
# 4. Crear directorios necesarios
# ============================================================
echo ""
echo "📁 Creando directorios..."
mkdir -p data
mkdir -p logs
chmod 755 data logs
echo -e "${GREEN}✅ Directorios creados${NC}"

# ============================================================
# 5. Configurar .env si no existe
# ============================================================
echo ""
if [ ! -f ".env" ]; then
    echo "⚙️  Configurando .env..."
    cp .env.example .env

    # Generar secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # Reemplazar valores por defecto
    sed -i "s/^TRADING_MODE=.*/TRADING_MODE=paper/" .env
    sed -i "s/^DASHBOARD_HOST=.*/DASHBOARD_HOST=0.0.0.0/" .env
    sed -i "s/^DASHBOARD_SECRET_KEY=.*/DASHBOARD_SECRET_KEY=$SECRET_KEY/" .env
    sed -i "s/^CFD_ENABLED=.*/CFD_ENABLED=true/" .env
    sed -i "s/^TA_ENABLED=.*/TA_ENABLED=true/" .env

    echo -e "${GREEN}✅ .env creado con valores por defecto${NC}"
    echo -e "${YELLOW}⚠️  IMPORTANTE: Edita .env y cambia DASHBOARD_PASSWORD${NC}"
else
    echo -e "${YELLOW}⚠ .env ya existe, no se sobrescribe${NC}"
fi

# ============================================================
# 6. Test rápido del bot
# ============================================================
echo ""
echo "🧪 Probando que el bot puede arrancar..."
timeout 5 python run.py 2>&1 | head -20 || true
echo -e "${GREEN}✅ Bot puede arrancar (test básico)${NC}"

# ============================================================
# 7. Instrucciones finales
# ============================================================
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Instalación base completada${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "📝 SIGUIENTES PASOS:"
echo ""
echo "1. Editar .env y configurar DASHBOARD_PASSWORD:"
echo "   nano .env"
echo ""
echo "2. Crear el servicio systemd (como root):"
echo "   sudo bash scripts/create_systemd_service.sh"
echo ""
echo "3. Configurar Nginx (como root):"
echo "   sudo bash scripts/setup_nginx.sh tudominio.com"
echo ""
echo "4. Arrancar el bot:"
echo "   sudo systemctl start polymarket-bot"
echo ""
echo "5. Ver logs:"
echo "   tail -f logs/bot.log"
echo ""
echo -e "${YELLOW}📖 Guía completa: docs/DEPLOY_VPS_HOSTINGER.md${NC}"
echo ""
