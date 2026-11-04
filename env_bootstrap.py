"""Load repo-root .env into os.environ for local CLI runs (optional python-dotenv)."""

import logging
import os
import sys
from pathlib import Path


def load_dotenv_if_present() -> None:
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_path, override=False)


def data_dir() -> Path:
    """Root for all persisted data: DATA_DIR (the image sets /data), else <repo>/data."""
    return Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent / "data")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def configure_logging_from_env() -> None:
    """
    Load .env, then set root log level and format.

    Environment:
      LOG_LEVEL — DEBUG, INFO, WARNING, ERROR (overrides flags below when set)
      PA_POWER_DEBUG — 1/true: enable DEBUG logging (verbose; includes other modules)
      DEBUG — same as PA_POWER_DEBUG (only used when LOG_LEVEL is unset)
    """
    load_dotenv_if_present()

    explicit = os.getenv("LOG_LEVEL", "").strip().upper()
    if explicit in logging._nameToLevel:
        level = logging._nameToLevel[explicit]
    elif _truthy_env("PA_POWER_DEBUG") or _truthy_env("DEBUG"):
        level = logging.DEBUG
    else:
        level = logging.INFO

    if level <= logging.DEBUG:
        fmt = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    else:
        fmt = "%(asctime)s - %(levelname)s - %(message)s"

    kwargs = {"level": level, "format": fmt}
    if sys.version_info >= (3, 8):
        kwargs["force"] = True
    logging.basicConfig(**kwargs)
