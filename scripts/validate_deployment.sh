#!/bin/bash

# ============================================================
# Script de validación post-despliegue
# Ejecutar como usuario polymarket
# ============================================================

# Colores
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}    VALIDACIÓN DEL DESPLIEGUE - POLYMARKET BOT${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo ""

ERRORS=0
WARNINGS=0

# ============================================================
# 1. Verificar directorio del proyecto
# ============================================================
echo "📁 Verificando estructura de directorios..."
if [ -f "run.py" ] && [ -f "bot/main.py" ]; then
    echo -e "${GREEN}✅ Proyecto encontrado${NC}"
else
    echo -e "${RED}❌ No se encuentra run.py o bot/main.py${NC}"
    ERRORS=$((ERRORS+1))
fi

# ============================================================
# 2. Verificar entorno virtual
# ============================================================
echo ""
echo "🐍 Verificando entorno virtual..."
if [ -d "venv" ] && [ -f "venv/bin/python" ]; then
    echo -e "${GREEN}✅ Entorno virtual existe${NC}"
    PYTHON_VERSION=$(venv/bin/python --version 2>&1)
    echo "   $PYTHON_VERSION"
else
    echo -e "${RED}❌ Entorno virtual no encontrado${NC}"
    ERRORS=$((ERRORS+1))
fi

# ============================================================
# 3. Verificar dependencias instaladas
# ============================================================
echo ""
echo "📦 Verificando dependencias..."
if [ -f "venv/bin/pip" ]; then
    PACKAGES=$(venv/bin/pip list | grep -E "flask|sqlalchemy|py-clob-client|websockets" | wc -l)
    if [ "$PACKAGES" -ge 4 ]; then
        echo -e "${GREEN}✅ Dependencias principales instaladas${NC}"
    else
        echo -e "${YELLOW}⚠️  Algunas dependencias podrían faltar${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
fi

# ============================================================
# 4. Verificar archivo .env
# ============================================================
echo ""
echo "⚙️  Verificando configuración .env..."
if [ -f ".env" ]; then
    echo -e "${GREEN}✅ .env existe${NC}"

    # Verificar campos críticos
    if grep -q "^TRADING_MODE=paper" .env; then
        echo "   ✓ TRADING_MODE=paper"
    else
        echo -e "${YELLOW}   ⚠️  TRADING_MODE no está en paper${NC}"
        WARNINGS=$((WARNINGS+1))
    fi

    if grep -q "^DASHBOARD_PASSWORD=.*[^ ]" .env; then
        echo "   ✓ DASHBOARD_PASSWORD configurado"
    else
        echo -e "${RED}   ❌ DASHBOARD_PASSWORD vacío${NC}"
        ERRORS=$((ERRORS+1))
    fi

    if grep -q "^DASHBOARD_SECRET_KEY=.*[^ ]" .env; then
        echo "   ✓ DASHBOARD_SECRET_KEY configurado"
    else
        echo -e "${YELLOW}   ⚠️  DASHBOARD_SECRET_KEY vacío${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
else
    echo -e "${RED}❌ .env no existe${NC}"
    ERRORS=$((ERRORS+1))
fi

# ============================================================
# 5. Verificar directorios de datos y logs
# ============================================================
echo ""
echo "📂 Verificando directorios..."
for dir in "data" "logs"; do
    if [ -d "$dir" ]; then
        echo -e "${GREEN}✅ $dir/ existe${NC}"
    else
        echo -e "${YELLOW}⚠️  $dir/ no existe (se creará al arrancar)${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
done

# ============================================================
# 6. Verificar servicio systemd
# ============================================================
echo ""
echo "🔧 Verificando servicio systemd..."
if systemctl list-unit-files | grep -q "polymarket-bot.service"; then
    echo -e "${GREEN}✅ Servicio systemd existe${NC}"

    STATUS=$(systemctl is-active polymarket-bot 2>/dev/null || echo "inactive")
    if [ "$STATUS" == "active" ]; then
        echo -e "${GREEN}   ✓ Bot está corriendo${NC}"
    else
        echo -e "${YELLOW}   ⚠️  Bot no está corriendo${NC}"
        echo "      Arrancar con: sudo systemctl start polymarket-bot"
        WARNINGS=$((WARNINGS+1))
    fi

    if systemctl is-enabled polymarket-bot &>/dev/null; then
        echo "   ✓ Auto-inicio habilitado"
    else
        echo -e "${YELLOW}   ⚠️  Auto-inicio deshabilitado${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
else
    echo -e "${YELLOW}⚠️  Servicio systemd no configurado${NC}"
    echo "   Crear con: sudo bash scripts/create_systemd_service.sh"
    WARNINGS=$((WARNINGS+1))
fi

# ============================================================
# 7. Verificar Nginx
# ============================================================
echo ""
echo "🌐 Verificando Nginx..."
if systemctl is-active nginx &>/dev/null; then
    echo -e "${GREEN}✅ Nginx está corriendo${NC}"

    if [ -f "/etc/nginx/sites-enabled/polymarket" ]; then
        echo "   ✓ Configuración de polymarket existe"

        # Extraer dominio
        DOMAIN=$(grep "server_name" /etc/nginx/sites-enabled/polymarket | awk '{print $2}' | tr -d ';')
        if [ ! -z "$DOMAIN" ]; then
            echo "   ✓ Dominio: $DOMAIN"

            # Verificar SSL
            if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
                echo -e "${GREEN}   ✓ SSL configurado${NC}"
            else
                echo -e "${YELLOW}   ⚠️  SSL no configurado${NC}"
                WARNINGS=$((WARNINGS+1))
            fi
        fi
    else
        echo -e "${YELLOW}   ⚠️  Configuración de polymarket no encontrada${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
else
    echo -e "${YELLOW}⚠️  Nginx no está corriendo${NC}"
    WARNINGS=$((WARNINGS+1))
fi

# ============================================================
# 8. Verificar conectividad del bot
# ============================================================
echo ""
echo "🔌 Verificando conectividad local..."
if curl -s http://localhost:5000/healthz > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Dashboard responde en localhost:5000${NC}"
else
    echo -e "${YELLOW}⚠️  Dashboard no responde (el bot podría no estar corriendo)${NC}"
    WARNINGS=$((WARNINGS+1))
fi

# ============================================================
# 9. Verificar base de datos
# ============================================================
echo ""
echo "🗄️  Verificando base de datos..."
if [ -f "data/streak_snapper.db" ]; then
    echo -e "${GREEN}✅ Base de datos existe${NC}"

    # Contar trades
    TRADES=$(sqlite3 data/streak_snapper.db "SELECT COUNT(*) FROM trades;" 2>/dev/null || echo "0")
    echo "   📊 Trades en DB: $TRADES"
else
    echo -e "${YELLOW}⚠️  Base de datos no existe (se creará al arrancar)${NC}"
    WARNINGS=$((WARNINGS+1))
fi

# ============================================================
# 10. Verificar logs recientes
# ============================================================
echo ""
echo "📝 Verificando logs..."
if [ -f "logs/bot.log" ]; then
    echo -e "${GREEN}✅ Log file existe${NC}"

    # Últimas líneas del log
    LAST_LOG=$(tail -1 logs/bot.log 2>/dev/null)
    if [ ! -z "$LAST_LOG" ]; then
        echo "   Última entrada: $(echo "$LAST_LOG" | cut -c1-60)..."
    fi

    # Buscar errores recientes
    RECENT_ERRORS=$(tail -100 logs/bot.log 2>/dev/null | grep -i "error\|exception" | wc -l)
    if [ "$RECENT_ERRORS" -gt 0 ]; then
        echo -e "${YELLOW}   ⚠️  $RECENT_ERRORS errores en las últimas 100 líneas${NC}"
        WARNINGS=$((WARNINGS+1))
    fi
else
    echo -e "${YELLOW}⚠️  Log file no existe${NC}"
    WARNINGS=$((WARNINGS+1))
fi

# ============================================================
# RESUMEN FINAL
# ============================================================
echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}                    RESUMEN${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo ""

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}✅ TODO CORRECTO - Despliegue exitoso${NC}"
    echo ""
    echo "🎉 El bot está listo para operar en paper mode"
    echo ""
    echo "📊 Accede al dashboard:"
    if [ ! -z "$DOMAIN" ]; then
        if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
            echo "   https://$DOMAIN"
        else
            echo "   http://$DOMAIN"
        fi
    else
        echo "   http://$(hostname -I | awk '{print $1}'):5000"
    fi
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}⚠️  DESPLIEGUE FUNCIONAL CON ADVERTENCIAS${NC}"
    echo ""
    echo "Advertencias encontradas: $WARNINGS"
    echo "El bot puede funcionar pero revisa las advertencias arriba"
else
    echo -e "${RED}❌ ERRORES ENCONTRADOS - Revisar configuración${NC}"
    echo ""
    echo "Errores: $ERRORS"
    echo "Advertencias: $WARNINGS"
    echo ""
    echo "Revisa la guía: docs/DEPLOY_VPS_HOSTINGER.md"
fi

echo ""
echo "📝 Comandos útiles:"
echo "   Estado:   sudo systemctl status polymarket-bot"
echo "   Logs:     tail -f logs/bot.log"
echo "   Reiniciar: sudo systemctl restart polymarket-bot"
echo ""

exit $ERRORS
