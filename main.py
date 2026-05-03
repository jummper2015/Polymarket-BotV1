"""Root entry point used by gunicorn and ``python main.py``.

gunicorn expects ``main:app`` — so we expose the Flask application as ``app``.
Running ``python main.py`` (or ``python run.py``) boots the full bot + dashboard.
"""
from bot.dashboard import create_app

# Flask application exposed for gunicorn (``main:app``)
app = create_app()


def main() -> None:
    from bot.main import main as _boot
    _boot()


if __name__ == "__main__":
    main()
