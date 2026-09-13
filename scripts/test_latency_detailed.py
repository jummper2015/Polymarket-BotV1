#!/usr/bin/env python3
"""
Test de latencia detallado para Polymarket Trading Bot
Ejecutar desde tu VPS para determinar si la ubicación es óptima

Usage:
    python3 scripts/test_latency_detailed.py
"""

import time
import requests
import statistics
import sys
from typing import Dict, List

# Colores ANSI
class Colors:
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    RED = '\033[0;31m'
    BLUE = '\033[0;34m'
    BOLD = '\033[1m'
    NC = '\033[0m'  # No Color


def test_endpoint(name: str, url: str, num_requests: int = 20) -> Dict:
    """Test de latencia a un endpoint específico."""
    print(f"\n{Colors.BLUE}🎯 Testing {name}...{Colors.NC}")

    latencies = []
    errors = 0

    for i in range(num_requests):
        try:
            start = time.time()
            response = requests.get(url, timeout=5)
            latency = (time.time() - start) * 1000  # convertir a ms
            latencies.append(latency)

            # Progress indicator
            if (i + 1) % 5 == 0:
                print(f"   Progress: {i + 1}/{num_requests}", end='\r')

            time.sleep(0.1)  # No saturar el endpoint

        except requests.exceptions.Timeout:
            errors += 1
            print(f"   {Colors.YELLOW}⚠️  Request {i+1} timeout{Colors.NC}")
        except requests.exceptions.RequestException as e:
            errors += 1
            print(f"   {Colors.RED}❌ Request {i+1} failed: {e}{Colors.NC}")

    if not latencies:
        return {
            'success': False,
            'errors': errors,
            'message': 'All requests failed'
        }

    # Calcular estadísticas
    avg = statistics.mean(latencies)
    median = statistics.median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
    p99 = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 1 else latencies[0]
    min_lat = min(latencies)
    max_lat = max(latencies)
    stdev = statistics.stdev(latencies) if len(latencies) > 1 else 0

    return {
        'success': True,
        'avg': avg,
        'median': median,
        'p95': p95,
        'p99': p99,
        'min': min_lat,
        'max': max_lat,
        'stdev': stdev,
        'errors': errors,
        'total_requests': num_requests
    }


def print_results(name: str, results: Dict):
    """Imprimir resultados formateados."""
    if not results['success']:
        print(f"\n{Colors.RED}❌ {name}: {results['message']}{Colors.NC}")
        return

    avg = results['avg']

    # Determinar color basado en latencia promedio
    if avg < 50:
        color = Colors.GREEN
        status = "✅"
    elif avg < 100:
        color = Colors.GREEN
        status = "✅"
    elif avg < 200:
        color = Colors.YELLOW
        status = "⚠️"
    else:
        color = Colors.RED
        status = "❌"

    print(f"\n{status} {Colors.BOLD}{name}{Colors.NC}")
    print(f"   Promedio: {color}{avg:6.1f}ms{Colors.NC}")
    print(f"   Mediana:  {median:6.1f}ms")
    print(f"   P95:      {results['p95']:6.1f}ms")
    print(f"   P99:      {results['p99']:6.1f}ms")
    print(f"   Rango:    {results['min']:6.1f}ms - {results['max']:6.1f}ms")
    print(f"   StdDev:   {results['stdev']:6.1f}ms")

    if results['errors'] > 0:
        print(f"   {Colors.YELLOW}⚠️  Errores: {results['errors']}/{results['total_requests']}{Colors.NC}")


def get_location_info():
    """Obtener información de ubicación del servidor."""
    try:
        response = requests.get('https://ipapi.co/json/', timeout=5)
        data = response.json()
        return {
            'city': data.get('city', 'N/A'),
            'region': data.get('region', 'N/A'),
            'country': data.get('country_name', 'N/A'),
            'latitude': data.get('latitude', 'N/A'),
            'longitude': data.get('longitude', 'N/A'),
            'org': data.get('org', 'N/A'),
            'timezone': data.get('timezone', 'N/A')
        }
    except Exception as e:
        return None


def analyze_and_recommend(clob_results: Dict, gamma_results: Dict, binance_results: Dict):
    """Analizar resultados y dar recomendaciones."""
    print(f"\n{Colors.BLUE}{'='*60}{Colors.NC}")
    print(f"{Colors.BOLD}📊 ANÁLISIS Y RECOMENDACIÓN{Colors.NC}")
    print(f"{Colors.BLUE}{'='*60}{Colors.NC}\n")

    if not clob_results['success']:
        print(f"{Colors.RED}❌ No se pudo conectar a Polymarket CLOB{Colors.NC}")
        return

    clob_avg = clob_results['avg']

    # Evaluar ubicación
    print(f"📈 Latencia clave (Polymarket CLOB): {Colors.BOLD}{clob_avg:.1f}ms{Colors.NC}\n")

    if clob_avg < 30:
        print(f"{Colors.GREEN}✅ EXCELENTE - Ubicación óptima{Colors.NC}\n")
        print("Tu VPS está en una ubicación ideal para trading en Polymarket.")
        print("Probablemente estás en US East (Virginia/Nueva York).\n")
        print("📋 Estrategias viables:")
        print(f"   {Colors.GREEN}✅ near_res{Colors.NC}        - Ventaja competitiva máxima")
        print(f"   {Colors.GREEN}✅ box_builder{Colors.NC}     - Repricing rápido")
        print(f"   {Colors.GREEN}✅ temporal_arb{Colors.NC}    - Detección temprana")
        print(f"   {Colors.GREEN}✅ coin_flip_dog{Colors.NC}   - Sin problemas")

    elif clob_avg < 80:
        print(f"{Colors.GREEN}✅ BUENO - Ubicación viable{Colors.NC}\n")
        print("Tu ubicación es buena para la mayoría de estrategias.")
        print("Probablemente estás en US West o EU West.\n")
        print("📋 Estrategias viables:")
        print(f"   {Colors.GREEN}✅ near_res{Colors.NC}        - Viable (ligera desventaja vs US East)")
        print(f"   {Colors.GREEN}✅ box_builder{Colors.NC}     - Viable")
        print(f"   {Colors.GREEN}✅ temporal_arb{Colors.NC}    - Sin problemas")
        print(f"   {Colors.GREEN}✅ coin_flip_dog{Colors.NC}   - Sin problemas")

    elif clob_avg < 150:
        print(f"{Colors.YELLOW}⚠️  ACEPTABLE - Ubicación subóptima{Colors.NC}\n")
        print("Tu ubicación funciona pero no es ideal.")
        print("Probablemente estás en Europa o South America.\n")
        print("📋 Estrategias viables:")
        print(f"   {Colors.YELLOW}⚠️  near_res{Colors.NC}        - Desventaja significativa")
        print(f"   {Colors.YELLOW}⚠️  box_builder{Colors.NC}     - Repricing lento")
        print(f"   {Colors.GREEN}✅ temporal_arb{Colors.NC}    - Viable")
        print(f"   {Colors.GREEN}✅ coin_flip_dog{Colors.NC}   - Viable")
        print(f"\n{Colors.YELLOW}💡 Recomendación: Considera migrar a US East para mejor performance{Colors.NC}")

    else:
        print(f"{Colors.RED}❌ PROBLEMÁTICO - Latencia muy alta{Colors.NC}\n")
        print("Tu ubicación NO es adecuada para trading competitivo.")
        print("Probablemente estás en Asia o muy lejos de US East.\n")
        print("📋 Estrategias viables:")
        print(f"   {Colors.RED}❌ near_res{Colors.NC}        - NO viable")
        print(f"   {Colors.RED}❌ box_builder{Colors.NC}     - NO competitivo")
        print(f"   {Colors.YELLOW}⚠️  temporal_arb{Colors.NC}    - Limitado")
        print(f"   {Colors.YELLOW}⚠️  coin_flip_dog{Colors.NC}   - Marginal")
        print(f"\n{Colors.RED}⚠️  Recomendación URGENTE: MIGRAR a US East{Colors.NC}")

    # Análisis de estabilidad
    print(f"\n{Colors.BLUE}{'─'*60}{Colors.NC}")
    print(f"{Colors.BOLD}📊 Análisis de Estabilidad{Colors.NC}\n")

    clob_stdev = clob_results['stdev']
    clob_p95 = clob_results['p95']

    if clob_stdev < 20:
        print(f"{Colors.GREEN}✅ Conexión estable{Colors.NC} (StdDev: {clob_stdev:.1f}ms)")
    elif clob_stdev < 50:
        print(f"{Colors.YELLOW}⚠️  Conexión moderadamente variable{Colors.NC} (StdDev: {clob_stdev:.1f}ms)")
    else:
        print(f"{Colors.RED}❌ Conexión inestable{Colors.NC} (StdDev: {clob_stdev:.1f}ms)")

    if clob_p95 / clob_avg < 1.5:
        print(f"{Colors.GREEN}✅ Latencia consistente{Colors.NC} (P95/Avg ratio: {clob_p95/clob_avg:.2f})")
    else:
        print(f"{Colors.YELLOW}⚠️  Picos de latencia ocasionales{Colors.NC} (P95/Avg ratio: {clob_p95/clob_avg:.2f})")

    # Recomendaciones de ubicación
    print(f"\n{Colors.BLUE}{'─'*60}{Colors.NC}")
    print(f"{Colors.BOLD}📍 Ubicaciones Recomendadas{Colors.NC}\n")
    print("1. 🏆 US East (Virginia/Nueva York)    < 30ms   ⭐⭐⭐⭐⭐")
    print("2. ✅ US West (Los Angeles)            50-70ms  ⭐⭐⭐⭐")
    print("3. ✅ EU West (Frankfurt/Amsterdam)    80-120ms ⭐⭐⭐")
    print("4. ⚠️  South America (São Paulo)       120-180ms ⭐⭐")
    print("5. ❌ Asia Pacific (Singapur/Tokio)    180-250ms ⭐")

    print(f"\n{Colors.BLUE}{'─'*60}{Colors.NC}")
    print(f"\n📖 Documentación completa: {Colors.BOLD}docs/VPS_LOCATION_LATENCY.md{Colors.NC}")


def main():
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.NC}")
    print(f"{Colors.BOLD}🌐 Test de Latencia Detallado - Polymarket Bot{Colors.NC}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.NC}\n")

    # Detectar ubicación
    print(f"{Colors.BLUE}📍 Detectando ubicación del servidor...{Colors.NC}")
    location = get_location_info()

    if location:
        print(f"\n{Colors.GREEN}✅ Ubicación detectada:{Colors.NC}")
        print(f"   Ciudad:     {location['city']}")
        print(f"   Región:     {location['region']}")
        print(f"   País:       {location['country']}")
        print(f"   Timezone:   {location['timezone']}")
        print(f"   ISP:        {location['org']}")
        print(f"   Coords:     {location['latitude']}, {location['longitude']}")
    else:
        print(f"\n{Colors.YELLOW}⚠️  No se pudo detectar la ubicación{Colors.NC}")

    # Endpoints a testear
    endpoints = {
        'Polymarket CLOB': 'https://clob.polymarket.com',
        'Polymarket Gamma': 'https://gamma-api.polymarket.com',
        'Binance API': 'https://api.binance.com/api/v3/ping',
    }

    print(f"\n{Colors.BLUE}{'─'*60}{Colors.NC}")
    print(f"{Colors.BOLD}📊 Ejecutando tests de latencia (20 requests por endpoint)...{Colors.NC}")

    # Ejecutar tests
    results = {}
    for name, url in endpoints.items():
        results[name] = test_endpoint(name, url, num_requests=20)
        print_results(name, results[name])

    # Análisis y recomendaciones
    analyze_and_recommend(
        results.get('Polymarket CLOB', {}),
        results.get('Polymarket Gamma', {}),
        results.get('Binance API', {})
    )

    print(f"\n{Colors.BLUE}{'='*60}{Colors.NC}\n")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}⚠️  Test interrumpido por el usuario{Colors.NC}\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n{Colors.RED}❌ Error: {e}{Colors.NC}\n")
        sys.exit(1)
