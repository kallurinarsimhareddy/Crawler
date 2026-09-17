"""Reading settings from the environment, with errors that name the variable."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple, TypeVar

__all__ = ["env_bool", "env_choice", "env_float", "env_int", "env_list", "env_optional", "env_path"]

T = TypeVar("T")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def env_optional(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name, "").strip()
    return value or None


def env_choice(env: Mapping[str, str], name: str, default: str, choices: Sequence[str]) -> str:
    value = env.get(name, default).strip().lower() or default
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, not {value!r}")
    return value


def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in env:
        return default
    value = env[name].strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, not {env[name]!r}")


def env_int(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    try:
        value = default if raw is None or not raw.strip() else int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a whole number, not {raw!r}") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_float(env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None or not raw.strip() else float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, not {raw!r}") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_list(env: Mapping[str, str], name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = env.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def env_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default
