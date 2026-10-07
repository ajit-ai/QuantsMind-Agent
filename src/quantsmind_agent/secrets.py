"""Secret storage.

Design rules enforced here:

* Secrets are stored **outside** the repository, in the OS user config dir.
* Secrets are never written to the application log.
* Secrets are never rendered in plain text by the GUI.
* Reading a secret returns a :class:`~pydantic.SecretStr` that masks repr/str.

Storage backends
-----------------
:class:`FileSecretStore`
    Writes ``secrets.json`` with restrictive permissions. This is the default
    because it works identically on Windows 11, Linux and macOS and needs no
    extra services.

:class:`KeyringSecretStore`
    Optional Windows Credential Manager / libsecret backend, used when
    ``keyring`` is importable and a credential is not already cached in the
    file store.

Both backends implement :class:`SecretStore`, so the rest of the application is
unaware of which one is active.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from quantsmind_agent.errors import ConfigurationError

__all__ = [
    "FileSecretStore",
    "KeyringSecretStore",
    "SecretStore",
    "build_secret_store",
    "mask_secret",
]

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "quantsmind-agent"


def mask_secret(value: str | None) -> str:
    """Return a UI-safe representation of ``value``.

    The result is safe to log and to display in the GUI: it reveals at most
    the first three and last two characters.
    """

    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 6}{value[-2:]}"


class SecretStore(ABC):
    """Persistence contract for credentials."""

    @abstractmethod
    def get(self, key: str) -> SecretStr | None:
        """Return the stored secret for ``key`` or ``None``."""

    @abstractmethod
    def set(self, key: str, value: SecretStr) -> None:
        """Persist ``value`` under ``key``."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove ``key``; returns ``True`` when something was removed."""

    @abstractmethod
    def keys(self) -> list[str]:
        """List the known secret keys (never the values)."""


def _restrict_permissions(path: Path) -> None:
    """Best-effort chmod so only the owning user can read the secret file."""

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        logger.debug("Could not restrict permissions on %s", path.name)


class FileSecretStore(SecretStore):
    """JSON-file backed store located in the per-user config directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Secret file is unreadable: {self._path.name}: {exc}",
                user_message=(
                    f"The credential file could not be read ({self._path.name}). "
                    "Delete it and re-enter your API key."
                ),
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(
                f"Secret file has an unexpected layout: {self._path.name}",
                user_message="The credential file is corrupted. Delete it and re-enter your API key.",
            )
        return {str(k): str(v) for k, v in raw.items()}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _restrict_permissions(tmp)
        os.replace(tmp, self._path)

    def get(self, key: str) -> SecretStr | None:
        value = self._load().get(key)
        return SecretStr(value) if value else None

    def set(self, key: str, value: SecretStr) -> None:
        data = self._load()
        data[key] = value.get_secret_value()
        self._save(data)

    def delete(self, key: str) -> bool:
        data = self._load()
        if key not in data:
            return False
        del data[key]
        self._save(data)
        return True

    def keys(self) -> list[str]:
        return sorted(self._load())


class KeyringSecretStore(SecretStore):
    """OS credential-manager backed store (Windows Credential Manager).

    Falls back to the file store when the platform has no usable keyring, so
    the application never crashes because of credential plumbing.
    """

    def __init__(self, fallback: SecretStore, username: str = "default") -> None:
        self._fallback = fallback
        self._username = username
        self._keyring: Any | None
        try:
            import keyring
        except Exception:
            logger.info("keyring is unavailable; using file-based secret storage")
            self._keyring = None
        else:
            self._keyring = keyring

    @property
    def _backend(self) -> bool:
        return self._keyring is not None

    def _name(self, key: str) -> str:
        return f"{self._username}/{key}"

    def get(self, key: str) -> SecretStr | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(_KEYRING_SERVICE, self._name(key))
            except Exception:
                logger.debug("keyring lookup failed for %s", key)
            else:
                if value:
                    return SecretStr(value)
        return self._fallback.get(key)

    def set(self, key: str, value: SecretStr) -> None:
        if self._keyring is not None:
            try:
                self._keyring.set_password(
                    _KEYRING_SERVICE, self._name(key), value.get_secret_value()
                )
                return
            except Exception:
                logger.debug("keyring write failed for %s; using file fallback", key)
        self._fallback.set(key, value)

    def delete(self, key: str) -> bool:
        removed = False
        if self._keyring is not None:
            try:
                self._keyring.delete_password(_KEYRING_SERVICE, self._name(key))
                removed = True
            except Exception:
                logger.debug("keyring delete failed for %s; using file fallback", key)
        return self._fallback.delete(key) or removed

    def keys(self) -> list[str]:
        # Keyring backends do not offer portable enumeration across Windows
        # Credential Manager and libsecret, so only the file-backed half of the
        # store is enumerable. That is sufficient for the v0.1 Settings UI.
        return sorted(set(self._fallback.keys()))


def build_secret_store(path: Path, *, prefer_keyring: bool = True) -> SecretStore:
    """Return the best available :class:`SecretStore` for ``path``."""

    file_store = FileSecretStore(path)
    if prefer_keyring:
        return KeyringSecretStore(file_store)
    return file_store
