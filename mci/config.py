"""Konfiguration aus .env (gitignored). Zugangsdaten werden nie ausgegeben."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"  # Cache/Tokens, gitignored
DATA_DIR.mkdir(exist_ok=True)

SAKAI_BASE = "https://sakai.mci4me.at"
MYMCI_API = "https://callmyapi.mci4me.at/api/my/5"
MYMCI_ORIGIN = "https://my.mci4me.at"


def user() -> str:
    return os.getenv("MCI_USER", "").strip()


def password() -> str:
    return os.getenv("MCI_PASS", "")


def sakai_cookie() -> str:
    return os.getenv("SAKAI_COOKIE", "").strip()


def files_dir() -> Path:
    """Uni-Wurzel. Kurse landen in <n>_Semester/Sakai/<Kurs>/ (siehe util.course_info)."""
    raw = os.getenv("MCI_UNI_DIR", "~/Me/Uni")
    return Path(os.path.expanduser(raw))


class ConfigError(RuntimeError):
    pass


def require_credentials() -> tuple[str, str]:
    u, p = user(), password()
    if not u or not p:
        raise ConfigError(
            "MCI_USER/MCI_PASS fehlen. .env.example nach .env kopieren und selbst ausfüllen."
        )
    return u, p


class AuthError(RuntimeError):
    """Login fehlgeschlagen oder Session abgelaufen."""
