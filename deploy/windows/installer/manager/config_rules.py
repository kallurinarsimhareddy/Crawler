"""Input rules for the SANA GTM setup wizard (and the unattended --import-env path).

Pure functions, no GUI and no network, so every rule is unit-tested. A problem is
``{"field": ..., "message": ...}``; messages describe what is wrong with a value
**without ever repeating the value** -- secrets typed into the wizard must not end
up in a dialog, the setup log or a test failure.

Wizard steps (:func:`wizard_steps`):

    first install:   welcome -> folder -> configuration -> review -> install -> finish
    update:          welcome -> install -> finish
    update + change: welcome -> configuration -> review -> install -> finish
"""

from __future__ import annotations

import os
import re
from pathlib import PureWindowsPath
from typing import Dict, List, Optional
from urllib.parse import urlsplit

__all__ = ["wizard_steps", "check_config", "check_install_dir", "problem_messages", "mask"]

_SUPABASE_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}\.supabase\.(co|in)$")
_JWT = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
_SB_KEY = re.compile(r"^sb_(publishable|secret)_[A-Za-z0-9_-]{16,}$")
_UPSTASH_TOKEN = re.compile(r"^[A-Za-z0-9_=-]{20,}$")
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SYSTEM_DIRS = ("windows", "program files", "program files (x86)", "programdata")


def wizard_steps(existing: bool, change_config: bool = False) -> List[str]:
    if not existing:
        return ["welcome", "folder", "configuration", "review", "install", "finish"]
    if change_config:
        return ["welcome", "configuration", "review", "install", "finish"]
    return ["welcome", "install", "finish"]


def mask(value: Optional[str]) -> str:
    """How a secret is shown on the review page: never the value, only that it is set."""
    return "saved / entered (hidden)" if value else "not set"


def _problem(field: str, message: str) -> Dict[str, str]:
    return {"field": field, "message": message}


def _check_supabase_url(url: str) -> Optional[str]:
    if not url:
        return "Supabase project URL is required"
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "Supabase project URL must start with https:// (e.g. https://<ref>.supabase.co)"
    if parts.path not in ("", "/") or parts.query or parts.username or parts.password:
        return "Supabase project URL must be just https://<project-ref>.supabase.co (no path or credentials)"
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return "Supabase project URL has no valid host name"
    if host.endswith(".supabase.co") or host.endswith(".supabase.in"):
        if not _SUPABASE_HOST.match(host):
            return "Supabase project reference must be lowercase letters and digits (<ref>.supabase.co)"
    return None   # a custom domain in front of Supabase is allowed


def _check_database_url(url: str) -> Optional[str]:
    if not url:
        return "Supabase database connection string is required"
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        return "Supabase database connection string must start with postgresql://"
    if not parts.hostname:
        return "Supabase database connection string has no host"
    if not parts.password:
        return "Supabase database connection string has no password"
    if parts.hostname in ("localhost", "127.0.0.1"):
        return "Supabase database connection string points at this PC, not Supabase"
    return None


def _check_redis_url(url: str) -> Optional[str]:
    if not url:
        return "Upstash Redis URL is required"
    parts = urlsplit(url)
    if parts.scheme not in ("redis", "rediss"):
        return "Upstash Redis URL must start with rediss://"
    if not parts.hostname:
        return "Upstash Redis URL has no host"
    if (parts.hostname or "").endswith(".upstash.io"):
        if parts.scheme != "rediss":
            return "Upstash requires TLS: use rediss:// (not redis://)"
        if not parts.password:
            return "Upstash Redis URL has no password (rediss://default:<password>@<host>:6379)"
    return None


def _check_upstash_rest(url: str, token: str) -> List[Dict[str, str]]:
    out = []
    if url:
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(".upstash.io"):
            out.append(_problem("upstash_rest_url", "Upstash REST URL must look like https://<name>.upstash.io"))
    if token and not _UPSTASH_TOKEN.match(token):
        out.append(_problem("UPSTASH_REST_TOKEN", "Upstash REST token has an unexpected format"))
    if bool(url) != bool(token) and (url or token):
        out.append(_problem("upstash_rest_url", "Upstash REST URL and token go together; enter both or neither"))
    return out


def _check_supabase_key(key: str) -> Optional[str]:
    if key and not (_JWT.match(key) or _SB_KEY.match(key)):
        return "Supabase key must be a JWT (eyJ...) or an sb_publishable_/sb_secret_ key"
    return None


def _check_port(port) -> Optional[str]:
    try:
        value = int(str(port).strip())
    except (TypeError, ValueError):
        return "Local API port must be a number"
    if not 1024 <= value <= 65535:
        return "Local API port must be between 1024 and 65535"
    return None


def check_install_dir(path: str, *, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """None when ``path`` is a sensible per-user install folder."""
    env = env if env is not None else dict(os.environ)
    if not path or not path.strip():
        return "Install folder is required"
    p = PureWindowsPath(path.strip())
    if not p.is_absolute() or not p.drive:
        return "Install folder must be a full path such as C:\\Users\\you\\AppData\\Local\\Programs\\SANA GTM"
    if str(p).startswith("\\\\"):
        return "Install folder must be on a local drive, not a network share"
    if len(str(p)) > 150:
        return "Install folder path is too long (150 characters at most)"
    if any(ch in str(p)[len(p.drive):] for ch in '<>:"|?*'):
        return "Install folder contains characters Windows does not allow"
    for part in p.parts[1:]:
        if part.split(".")[0].lower() in _RESERVED_NAMES:
            return "Install folder uses a name Windows reserves (CON, NUL, COM1...)"
    if len(p.parts) < 2:
        return "Install folder cannot be the root of a drive"
    top = p.parts[1].lower()
    if top in _SYSTEM_DIRS:
        return "Install folder must not be inside Windows or Program Files (no administrator rights are used)"
    return None


def check_config(settings: Dict, secrets: Dict, *, install_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """Every problem with the wizard's values. Empty list = OK to install."""
    out: List[Dict[str, str]] = []
    for field, message in (
        ("supabase_url", _check_supabase_url((settings.get("supabase_url") or "").strip())),
        ("CAREERCLOUD_DATABASE_URL", _check_database_url((secrets.get("CAREERCLOUD_DATABASE_URL") or "").strip())),
        ("CAREERCLOUD_REDIS_URL", _check_redis_url((secrets.get("CAREERCLOUD_REDIS_URL") or "").strip())),
        ("supabase_anon_key", _check_supabase_key((settings.get("supabase_anon_key") or "").strip())),
        ("api_port", _check_port(settings.get("api_port", 8100))),
    ):
        if message:
            out.append(_problem(field, message))
    out += _check_upstash_rest((settings.get("upstash_rest_url") or "").strip(),
                               (secrets.get("UPSTASH_REST_TOKEN") or "").strip())
    if len((secrets.get("CAREERCLOUD_PLATFORM_SECRETS_KEY") or "").strip()) < 16:
        out.append(_problem("CAREERCLOUD_PLATFORM_SECRETS_KEY",
                            "SANA GTM platform secret is required (at least 16 characters)"))
    queue_prefix = settings.get("queue_prefix")
    if queue_prefix is not None and not re.match(r"^[a-z0-9][a-z0-9:_-]{0,62}$", str(queue_prefix)):
        out.append(_problem("queue_prefix", "Queue prefix may use lowercase letters, digits, ':', '_' and '-'"))
    if settings.get("tunnel_mode") == "named" and not (secrets.get("TUNNEL_TOKEN") and settings.get("tunnel_hostname")):
        out.append(_problem("TUNNEL_TOKEN", "A named Cloudflare tunnel needs both the tunnel token and its public hostname"))
    if install_dir is not None:
        message = check_install_dir(install_dir)
        if message:
            out.append(_problem("install_dir", message))
    return out


def problem_messages(problems: List[Dict[str, str]]) -> List[str]:
    return [p["message"] for p in problems]
