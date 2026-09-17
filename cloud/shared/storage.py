"""Object storage for result files.

:class:`ObjectStorage` is what the worker writes results to and the API streams
downloads from. :class:`LocalFileStorage` keeps objects under one directory for
local development; a Supabase Storage / S3 implementation replaces it on
deployment without either side changing.

**Keys are never paths a user chose.** They are built by the worker from ids the
database generated (``results/<owner>/<job_id>/<file>``), validated against a
strict pattern, and resolved inside the storage root. A download request names
a result *id*; the key comes from the database row, which is only returned to
the job's owner.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

__all__ = ["InvalidKeyError", "LocalFileStorage", "ObjectStorage", "StoredObject", "validate_key"]

_KEY = re.compile(r"^[a-z0-9][a-z0-9_\-]*(/[a-z0-9][a-z0-9_\-.]*){1,6}$")
_CHUNK = 1024 * 1024


class InvalidKeyError(ValueError):
    """A storage key that could escape its root or name something unexpected."""


def validate_key(key: str) -> str:
    if not isinstance(key, str) or len(key) > 512 or not _KEY.match(key) or ".." in key:
        raise InvalidKeyError(f"invalid storage key: {key!r}")
    return key


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class ObjectStorage(ABC):
    name: str = "storage"

    @abstractmethod
    def put_file(self, key: str, source: Path, *, content_type: str) -> StoredObject:
        """Upload ``source`` under ``key``, replacing any existing object."""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Open an object for reading. Raises :class:`FileNotFoundError` if absent."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove an object if present."""

    def iter_bytes(self, key: str) -> Iterator[bytes]:
        with self.open(key) as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    return
                yield chunk


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalFileStorage(ObjectStorage):
    """Objects as files under ``root``. Writes are atomic (copy, then rename)."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        path = (self._root / validate_key(key)).resolve()
        if self._root not in path.parents:
            raise InvalidKeyError(f"storage key escapes the root: {key!r}")
        return path

    def put_file(self, key: str, source: Path, *, content_type: str) -> StoredObject:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        shutil.copyfile(source, partial)
        partial.replace(target)
        return StoredObject(key=key, size_bytes=target.stat().st_size, sha256=_sha256(target))

    def open(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
