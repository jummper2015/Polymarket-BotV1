"""Convenience wrapper so `python run.py` boots the bot and dashboard."""
import os
from pathlib import Path

# IMPORTANTE: Cargar .env ANTES de importar cualquier módulo del bot
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / '.env'
    loaded = load_dotenv(env_path, override=True)
    
    # Verificar que se cargó
    ta_enabled = os.getenv('TA_ENABLED')
    print(f"[run.py] .env loaded: {loaded}")
    print(f"[run.py] TA_ENABLED = {ta_enabled}")
    print(f"[run.py] TA_MIN_ITM_PCT from env: {os.getenv("TA_MIN_ITM_PCT")}")
    print(f"[run.py] TA_MIN_ASK from env: {os.getenv("TA_MIN_ASK")}")
    print(f"[run.py] TA_MAX_ASK from env: {os.getenv("TA_MAX_ASK")}")
    
except ImportError as e:
    print(f"[run.py] python-dotenv not available: {e}")

# Ahora sí importar el bot (después de cargar .env)
from bot.main import main

if __name__ == "__main__":
    main()
