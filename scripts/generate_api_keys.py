#!/usr/bin/env python3
"""
Script para generar credenciales CLOB API de Polymarket.

Requisitos:
- py-clob-client instalado: pip install py-clob-client
- Tener la private key de tu wallet
"""

import sys
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

def generate_credentials():
    """
    Genera las credenciales API para Polymarket CLOB.
    """
    print("=" * 80)
    print("GENERADOR DE CREDENCIALES POLYMARKET CLOB API")
    print("=" * 80)
    print()

    # Solicitar private key
    print("⚠️  ADVERTENCIA: Nunca compartas tu private key con nadie.")
    print("   Este script solo la usa localmente para generar las credenciales.")
    print()
    private_key = input("Ingresa tu private key (sin 0x prefix): ").strip()

    # Limpiar si tiene 0x
    if private_key.startswith("0x"):
        private_key = private_key[2:]

    # Validar longitud
    if len(private_key) != 64:
        print(f"❌ Error: Private key debe tener 64 caracteres (tiene {len(private_key)})")
        sys.exit(1)

    print("\n🔄 Generando credenciales...")

    try:
        # Crear cliente CLOB
        host = "https://clob.polymarket.com"
        chain_id = POLYGON  # 137

        # Crear cliente con la private key
        client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id
        )

        # Derivar credenciales
        creds = client.create_or_derive_api_creds()

        print("\n✅ Credenciales generadas exitosamente!")
        print("=" * 80)
        print("\n📋 CREDENCIALES POLYMARKET CLOB API")
        print("─" * 80)
        print(f"API Key:      {creds['apiKey']}")
        print(f"Secret:       {creds['secret']}")
        print(f"Passphrase:   {creds['passphrase']}")
        print()
        print("⚠️  GUARDA ESTAS CREDENCIALES EN LUGAR SEGURO")
        print("   No las compartas con nadie.")
        print("=" * 80)

        # Mostrar wallet address
        from eth_account import Account
        account = Account.from_key(private_key)
        print(f"\n🔑 Wallet Address: {account.address}")
        print(f"   (Usa esta dirección como POLYMARKET_PROXY_ADDRESS)")
        print()

        # Generar snippet para .env
        print("\n📄 Agregar al .env:")
        print("─" * 80)
        print(f"POLYMARKET_API_KEY={creds['apiKey']}")
        print(f"POLYMARKET_SECRET={creds['secret']}")
        print(f"POLYMARKET_PASSPHRASE={creds['passphrase']}")
        print(f"POLYMARKET_PROXY_ADDRESS={account.address}")
        print("=" * 80)

    except Exception as e:
        print(f"\n❌ Error al generar credenciales: {e}")
        sys.exit(1)

if __name__ == "__main__":
    print()
    print("Este script genera credenciales API para operar en Polymarket.")
    print()

    # Verificar dependencias
    try:
        import py_clob_client
    except ImportError:
        print("❌ Error: py-clob-client no está instalado.")
        print("   Instala con: pip install py-clob-client")
        sys.exit(1)

    try:
        import eth_account
    except ImportError:
        print("❌ Error: eth-account no está instalado.")
        print("   Instala con: pip install eth-account")
        sys.exit(1)

    generate_credentials()
