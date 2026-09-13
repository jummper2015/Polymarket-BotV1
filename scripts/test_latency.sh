#!/bin/bash

# ============================================================
# Script de Test de Latencia para Polymarket Trading Bot
# Ejecutar desde tu VPS para determinar si la ubicación es óptima
# ============================================================

# Colores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🌐 Test de Latencia para Trading en Polymarket${NC}"
echo ""
echo "📍 Detectando ubicación del servidor..."
echo ""

# Detectar ubicación
LOCATION=$(curl -s https://ipapi.co/json/ 2>/dev/null)
if [ $? -eq 0 ]; then
    CITY=$(echo $LOCATION | grep -o '"city":"[^"]*"' | cut -d'"' -f4)
    COUNTRY=$(echo $LOCATION | grep -o '"country_name":"[^"]*"' | cut -d'"' -f4)
    REGION=$(echo $LOCATION | grep -o '"region":"[^"]*"' | cut -d'"' -f4)
    ORG=$(echo $LOCATION | grep -o '"org":"[^"]*"' | cut -d'"' -f4)

    echo -e "${GREEN}📌 Ubicación detectada:${NC}"
    echo "   Ciudad: $CITY"
    echo "   Región: $REGION"
    echo "   País: $COUNTRY"
    echo "   ISP: $ORG"
    echo ""
else
    echo -e "${YELLOW}⚠️  No se pudo detectar la ubicación${NC}"
    echo ""
fi

# ============================================================
# Test 1: ICMP Ping (básico pero rápido)
# ============================================================
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📊 TEST 1: ICMP Ping (10 paquetes)${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

echo "🎯 Polymarket CLOB:"
CLOB_PING=$(ping -c 10 clob.polymarket.com 2>/dev/null | tail -1 | awk '{print $4}' | cut -d '/' -f 2)
if [ -z "$CLOB_PING" ]; then
    echo -e "   ${RED}❌ No responde a ping${NC}"
else
    echo -e "   Promedio: ${GREEN}${CLOB_PING}ms${NC}"
fi
echo ""

echo "🎯 Polymarket Gamma:"
GAMMA_PING=$(ping -c 10 gamma-api.polymarket.com 2>/dev/null | tail -1 | awk '{print $4}' | cut -d '/' -f 2)
if [ -z "$GAMMA_PING" ]; then
    echo -e "   ${RED}❌ No responde a ping${NC}"
else
    echo -e "   Promedio: ${GREEN}${GAMMA_PING}ms${NC}"
fi
echo ""

echo "🎯 Binance API:"
BINANCE_PING=$(ping -c 10 api.binance.com 2>/dev/null | tail -1 | awk '{print $4}' | cut -d '/' -f 2)
if [ -z "$BINANCE_PING" ]; then
    echo -e "   ${RED}❌ No responde a ping${NC}"
else
    echo -e "   Promedio: ${GREEN}${BINANCE_PING}ms${NC}"
fi
echo ""

# ============================================================
# Test 2: HTTP Request (más preciso para APIs)
# ============================================================
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📊 TEST 2: HTTP Request (conexión real)${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

echo "🎯 Polymarket CLOB (5 requests):"
CLOB_TIMES=()
for i in {1..5}; do
    TIME=$(curl -o /dev/null -s -w "%{time_total}" https://clob.polymarket.com 2>/dev/null)
    if [ $? -eq 0 ]; then
        TIME_MS=$(echo "$TIME * 1000" | bc)
        CLOB_TIMES+=($TIME_MS)
        echo -e "   Request $i: ${TIME_MS}ms"
    fi
done
echo ""

echo "🎯 Polymarket Gamma (5 requests):"
GAMMA_TIMES=()
for i in {1..5}; do
    TIME=$(curl -o /dev/null -s -w "%{time_total}" https://gamma-api.polymarket.com 2>/dev/null)
    if [ $? -eq 0 ]; then
        TIME_MS=$(echo "$TIME * 1000" | bc)
        GAMMA_TIMES+=($TIME_MS)
        echo -e "   Request $i: ${TIME_MS}ms"
    fi
done
echo ""

echo "🎯 Binance API (5 requests):"
BINANCE_TIMES=()
for i in {1..5}; do
    TIME=$(curl -o /dev/null -s -w "%{time_total}" https://api.binance.com/api/v3/ping 2>/dev/null)
    if [ $? -eq 0 ]; then
        TIME_MS=$(echo "$TIME * 1000" | bc)
        BINANCE_TIMES+=($TIME_MS)
        echo -e "   Request $i: ${TIME_MS}ms"
    fi
done
echo ""

# ============================================================
# Análisis y Recomendación
# ============================================================
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📊 ANÁLISIS Y RECOMENDACIÓN${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Calcular promedio de CLOB HTTP (más importante)
if [ ${#CLOB_TIMES[@]} -gt 0 ]; then
    CLOB_AVG=$(printf '%s\n' "${CLOB_TIMES[@]}" | awk '{sum+=$1} END {print sum/NR}')
    CLOB_AVG_INT=${CLOB_AVG%.*}

    echo -e "📈 Latencia promedio Polymarket CLOB: ${GREEN}${CLOB_AVG}ms${NC}"
    echo ""

    if (( CLOB_AVG_INT < 30 )); then
        echo -e "${GREEN}✅ EXCELENTE${NC}"
        echo "   Ubicación óptima para trading en Polymarket"
        echo "   Todas las estrategias viables:"
        echo "   • near_res ✅"
        echo "   • box_builder ✅"
        echo "   • temporal_arb ✅"
        echo "   • coin_flip_dog ✅"
    elif (( CLOB_AVG_INT < 80 )); then
        echo -e "${GREEN}✅ BUENO${NC}"
        echo "   Ubicación viable para la mayoría de estrategias"
        echo "   Estrategias recomendadas:"
        echo "   • near_res ✅ (con ligera desventaja)"
        echo "   • box_builder ✅"
        echo "   • temporal_arb ✅"
        echo "   • coin_flip_dog ✅"
    elif (( CLOB_AVG_INT < 150 )); then
        echo -e "${YELLOW}⚠️  ACEPTABLE${NC}"
        echo "   Ubicación subóptima pero funcional"
        echo "   Estrategias recomendadas:"
        echo "   • near_res ⚠️ (en desventaja significativa)"
        echo "   • box_builder ⚠️"
        echo "   • temporal_arb ✅"
        echo "   • coin_flip_dog ✅"
        echo ""
        echo -e "${YELLOW}💡 Recomendación: Considera migrar a US East${NC}"
    else
        echo -e "${RED}❌ PROBLEMÁTICO${NC}"
        echo "   Latencia demasiado alta para trading competitivo"
        echo "   Solo estrategias muy tolerantes son viables"
        echo ""
        echo -e "${RED}⚠️  Recomendación: MIGRAR a US East urgentemente${NC}"
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "📍 Ubicaciones recomendadas:"
    echo "   1. US East (Virginia/Nueva York)    < 30ms   ⭐⭐⭐⭐⭐"
    echo "   2. US West (Los Angeles)            50-70ms  ⭐⭐⭐"
    echo "   3. EU West (Frankfurt/Amsterdam)    80-120ms ⭐⭐⭐"
    echo ""
    echo "📖 Documentación completa: docs/VPS_LOCATION_LATENCY.md"
else
    echo -e "${RED}❌ No se pudo completar el test de latencia${NC}"
fi

echo ""
