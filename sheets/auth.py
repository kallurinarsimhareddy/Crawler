"""Obtain credentials for the Google Sheets API, without ever storing a secret in source.

Two ways in, and the crawler prefers the first because the whole point of
version 3 is a job that runs on a Friday night with nobody watching::

    python -m sheets.inspect            # uses whichever is configured

**A service account** is a robot identity with its own key file. It never
expires, never opens a browser, and never asks anybody to sign in again, which
is what an unattended scheduled task needs. The operator shares the spreadsheet
with the service account's email address exactly as they would with a colleague.

**An installed-app OAuth flow** signs in as the operator themselves. It opens a
browser once and caches a token. Offered because it needs no sharing step, but
the token can be revoked or expire, and when it does the Friday run fails
silently until somebody notices — so it is the fallback, not the default.

Nothing here contains a credential. The key file's *path* comes from the
environment or a conventional location, and the file itself lives in
``secrets/``, which is listed in ``.gitignore`` precisely so that a key
downloaded into the project cannot reach the GitHub remote.

Configuration, in the order each is consulted:

===============================  =========================================
``CAREERCRAWLER_SPREADSHEET_ID``  The spreadsheet to open. Also accepts a
                                  full ``docs.google.com`` URL.
``CAREERCRAWLER_GOOGLE_CREDENTIALS``  Path to a service-account JSON key.
``GOOGLE_APPLICATION_CREDENTIALS``    The Google-standard equivalent.
``secrets/service_account.json``      Where the setup instructions put it.
``secrets/client_secret.json``        An OAuth client, for the fallback flow.
``secrets/token.json``                Where that flow caches its token.
===============================  =========================================

An ``.env`` file in the project root is read too, so an operator who would
rather not set Windows environment variables can put the same names in a file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Final, Optional, Sequence

from loguru import logger

__all__ = [
    "READONLY_SCOPES",
    "SCOPES",
    "CredentialsError",
    "SheetsCredentials",
    "load_env_file",
    "resolve_credentials",
    "resolve_spreadsheet_id",
]

#: Project root, so the conventional paths resolve from any working directory.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Full read and write access to the spreadsheets the identity can already see.
#: Deliberately the narrow ``spreadsheets`` scope rather than ``drive``: the
#: crawler needs to read and write one shared spreadsheet, and has no business
#: being able to enumerate or delete anything else in the operator's Drive.
SCOPES: Final[Sequence[str]] = ("https://www.googleapis.com/auth/spreadsheets",)

#: Read-only access, used by the inspection command so that a first connection
#: to a live spreadsheet cannot write to it even by mistake.
READONLY_SCOPES: Final[Sequence[str]] = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
)

#: Where a service-account key is looked for when nothing names one.
DEFAULT_SERVICE_ACCOUNT: Final[Path] = PROJECT_ROOT / "secrets" / "service_account.json"

#: Where an OAuth client and its cached token live.
DEFAULT_CLIENT_SECRET: Final[Path] = PROJECT_ROOT / "secrets" / "client_secret.json"
DEFAULT_TOKEN: Final[Path] = PROJECT_ROOT / "secrets" / "token.json"

#: Where an operator may put configuration instead of setting Windows variables.
DEFAULT_ENV_FILE: Final[Path] = PROJECT_ROOT / ".env"

#: A spreadsheet id inside a Google Sheets URL, so the operator can paste the
#: address bar rather than hunt for the id within it.
_ID_IN_URL: Final[re.Pattern[str]] = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")

#: What a bare spreadsheet id looks like.
_BARE_ID: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9-_]{20,}$")


class CredentialsError(RuntimeError):
    """No usable credentials were found, or they could not be loaded."""


@dataclass(frozen=True)
class SheetsCredentials:
    """Credentials, and an account of where they came from.

    Attributes:
        credentials: The Google credentials object.
        kind: ``"service-account"`` or ``"oauth"``.
        account: The identity's email address, when it can be determined.
            For a service account this is the address the spreadsheet must be
            shared with, so it is worth reporting in full.
        source: The file the credentials were loaded from.
        read_only: Whether they were requested with the read-only scope.
    """

    credentials: object
    kind: str
    account: str
    source: str
    read_only: bool = False

    def describe(self) -> str:
        """Render a one-line account, for the log and the setup report.

        Returns:
            Human-readable summary, naming no secret.
        """
        access = "read-only" if self.read_only else "read/write"
        return f"{self.kind} <{self.account or 'unknown'}> from {self.source} ({access})"


def load_env_file(path: Path | str = DEFAULT_ENV_FILE) -> Dict[str, str]:
    """Read ``KEY=value`` pairs from an ``.env`` file into the environment.

    Values already set in the real environment win, so a Windows environment
    variable is never silently overridden by a stale file.

    Args:
        path: The file. Missing is not an error — most installations set
            environment variables instead.

    Returns:
        The names that were loaded and their values, for logging. Empty when
        the file does not exist.
    """
    source = Path(path)
    if not source.is_file():
        return {}

    loaded: Dict[str, str] = {}

    try:
        # utils.encoding rather than a plain read: an .env file edited in
        # Notepad on Windows arrives with a BOM, and a BOM on the first line
        # makes the first variable's name unmatchable.
        from utils.encoding import read_text

        text = read_text(source).text
    except OSError as exc:
        logger.warning("Could not read {}: {}", source, exc)
        return {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if not name or name in os.environ:
            continue

        os.environ[name] = value
        loaded[name] = value

    if loaded:
        logger.debug("Loaded {} setting(s) from {}", len(loaded), source)

    return loaded


def resolve_spreadsheet_id(candidate: Optional[str] = None) -> str:
    """Work out which spreadsheet to open.

    Args:
        candidate: An id or a full Google Sheets URL, if one was given on the
            command line. When ``None``, ``CAREERCRAWLER_SPREADSHEET_ID`` is
            consulted.

    Returns:
        The bare spreadsheet id.

    Raises:
        CredentialsError: If no id is configured, or the value is not one.
    """
    load_env_file()

    raw = (candidate or os.environ.get("CAREERCRAWLER_SPREADSHEET_ID") or "").strip()

    if not raw:
        raise CredentialsError(
            "No spreadsheet configured. Set CAREERCRAWLER_SPREADSHEET_ID to the "
            "spreadsheet's id or its full URL — either as a Windows environment "
            f"variable or as a line in {DEFAULT_ENV_FILE}."
        )

    found = _ID_IN_URL.search(raw)
    if found:
        return found.group(1)

    if _BARE_ID.match(raw):
        return raw

    raise CredentialsError(
        f"{raw!r} is neither a spreadsheet id nor a Google Sheets URL. Expected "
        "something like 1LdOhoLxGY2wDSDRlEaTo7YZIunKkle0EoqZgjyDgvTM, or the "
        "whole https://docs.google.com/spreadsheets/d/... address."
    )


def _service_account_path() -> Optional[Path]:
    """Find a service-account key file, if one is configured or conventional.

    Returns:
        The path, or ``None`` when there is no such file.
    """
    for variable in ("CAREERCRAWLER_GOOGLE_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"):
        configured = (os.environ.get(variable) or "").strip()
        if configured:
            path = Path(configured)
            if path.is_file():
                return path
            logger.warning("{} points at {}, which does not exist", variable, path)

    return DEFAULT_SERVICE_ACCOUNT if DEFAULT_SERVICE_ACCOUNT.is_file() else None


def _load_service_account(path: Path, scopes: Sequence[str]) -> SheetsCredentials:
    """Load a service-account key.

    Args:
        path: The JSON key file.
        scopes: Scopes to request.

    Returns:
        The credentials.

    Raises:
        CredentialsError: If the file is not a usable service-account key.
    """
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise CredentialsError(
            "The Google API libraries are not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=list(scopes)
        )
    except (ValueError, KeyError) as exc:
        raise CredentialsError(
            f"{path} is not a valid service-account key file ({exc}). Download it "
            "again from Google Cloud Console: the service account's Keys tab, "
            "Add Key, Create new key, JSON."
        ) from exc
    except OSError as exc:
        raise CredentialsError(f"Could not read {path}: {exc}") from exc

    account = getattr(credentials, "service_account_email", "") or ""
    return SheetsCredentials(
        credentials=credentials,
        kind="service-account",
        account=account,
        source=str(path),
        read_only=list(scopes) == list(READONLY_SCOPES),
    )


def _load_oauth(scopes: Sequence[str]) -> SheetsCredentials:
    """Run or reuse the installed-app OAuth flow.

    Args:
        scopes: Scopes to request.

    Returns:
        The credentials.

    Raises:
        CredentialsError: If no OAuth client is configured, or the flow fails.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise CredentialsError(
            "The Google API libraries are not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    credentials = None

    if DEFAULT_TOKEN.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(DEFAULT_TOKEN), list(scopes))
        except (ValueError, OSError) as exc:
            logger.warning("Cached token at {} is unusable ({}); signing in again", DEFAULT_TOKEN, exc)
            credentials = None

    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - any refresh failure means sign in again
            logger.warning("Could not refresh the cached token ({}); signing in again", exc)
            credentials = None

    if credentials is None or not credentials.valid:
        if not DEFAULT_CLIENT_SECRET.is_file():
            raise CredentialsError(
                "No credentials found.\n\n"
                f"  Preferred: put a service-account key at {DEFAULT_SERVICE_ACCOUNT}\n"
                f"  Or:        put an OAuth client at {DEFAULT_CLIENT_SECRET}\n\n"
                "See the 'Configure Google Sheets access' section of README.md."
            )

        flow = InstalledAppFlow.from_client_secrets_file(str(DEFAULT_CLIENT_SECRET), list(scopes))
        # port=0 asks the OS for a free port, so the flow works on a machine
        # where something already holds the usual one.
        credentials = flow.run_local_server(port=0)

        try:
            DEFAULT_TOKEN.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_TOKEN.write_text(credentials.to_json(), encoding="utf-8")
        except OSError as exc:
            logger.warning("Signed in, but could not cache the token to {}: {}", DEFAULT_TOKEN, exc)

    return SheetsCredentials(
        credentials=credentials,
        kind="oauth",
        account=getattr(credentials, "client_id", "") or "signed-in user",
        source=str(DEFAULT_TOKEN if DEFAULT_TOKEN.is_file() else DEFAULT_CLIENT_SECRET),
        read_only=list(scopes) == list(READONLY_SCOPES),
    )


def resolve_credentials(read_only: bool = False) -> SheetsCredentials:
    """Find and load whatever credentials this installation is configured with.

    Args:
        read_only: Request the read-only scope, so an inspection cannot write.

    Returns:
        The credentials, with an account of where they came from.

    Raises:
        CredentialsError: If nothing usable is configured. The message names
            every place that was looked in.
    """
    load_env_file()

    scopes = READONLY_SCOPES if read_only else SCOPES

    key_file = _service_account_path()
    if key_file is not None:
        credentials = _load_service_account(key_file, scopes)
        logger.info("Google Sheets credentials: {}", credentials.describe())
        return credentials

    logger.debug("No service-account key found; trying the OAuth flow")
    credentials = _load_oauth(scopes)
    logger.info("Google Sheets credentials: {}", credentials.describe())
    return credentials


def build_service(read_only: bool = False, credentials: Optional[object] = None) -> object:
    """Build the Google Sheets API client.

    Args:
        read_only: Request the read-only scope.
        credentials: Pre-loaded credentials, to skip resolution.

    Returns:
        The ``spreadsheets`` service resource.

    Raises:
        CredentialsError: If credentials cannot be found or the client cannot
            be built.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise CredentialsError(
            "The Google API libraries are not installed. Run:\n"
            "    pip install -r requirements.txt"
        ) from exc

    resolved = credentials if credentials is not None else resolve_credentials(read_only).credentials

    try:
        # cache_discovery=False silences a noisy warning from oauth2client's
        # absent file cache, which this project does not use.
        return build("sheets", "v4", credentials=resolved, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001 - surfaced as one clear error
        raise CredentialsError(f"Could not build the Google Sheets client: {exc}") from exc
