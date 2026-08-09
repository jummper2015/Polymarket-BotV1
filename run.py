"""Convenience wrapper so `python run.py` boots the bot and dashboard."""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; env vars must be set another way

from bot.main import main

if __name__ == "__main__":
    main()
