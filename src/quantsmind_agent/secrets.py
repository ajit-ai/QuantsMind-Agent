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
    Optional Windows Credential Manager / libsecret backend. It is secure only
    when the keyring package has a usable backend; otherwise it reads existing
    plaintext values (so upgrading does not lose credentials) but refuses to
    write, reporting a :class:`SecretStoreError` instead.

Both backends implement :class:`SecretStore`, so the rest of the application is
unaware of which one is active. Neither silently writes a secret to a less
secure location than the one the caller selected.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from quantsmind_agent.errors import ConfigurationError, QuantsMindError

__all__ = [
    "FileSecretStore",
    "KeyringSecretStore",
    "SecretStore",
    "SecretStoreError",
    "StorageStatus",
    "build_secret_store",
    "mask_secret",
]

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "quantsmind-agent"


class SecretStoreError(QuantsMindError):
    """A secret-store operation failed and must not be papered over.

    Reads and writes that cannot reach the OS credential store are surfaced
    rather than silently redirected to the plaintext file. Callers decide how
    to degrade; the store never decides for them.
    """

    code = "secret_store_error"


class StorageStatus(StrEnum):
    """How securely the active backend keeps values."""

    SECURE = "secure"
    """An OS credential store is in use."""

    DEGRADED = "degraded"
    """A secure backend was expected but is not functioning."""

    PLAINTEXT = "plaintext"
    """Values live in the file store, which is not an OS credential store."""

    UNAVAILABLE = "unavailable"
    """No backend can be used."""

    NOT_APPLICABLE = "not_applicable"
    """The value did not come from a store (for example, the environment)."""


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

    @property
    def is_secure(self) -> bool:
        """Whether this backend keeps values in an OS credential store.

        ``False`` for a plaintext file. The credential vault refuses to write
        to an insecure store, because a file of API keys is a file of API
        keys whatever its permissions say.
        """

        return False

    @property
    def storage_status(self) -> StorageStatus:
        """How securely this backend keeps values.

        The base contract follows :attr:`is_secure`; a backend that can detect
        a *broken* secure store overrides it with :attr:`StorageStatus.DEGRADED`
        instead of pretending to be either secure or plaintext.
        """

        return StorageStatus.SECURE if self.is_secure else StorageStatus.PLAINTEXT

    def read(self, key: str) -> tuple[SecretStr | None, StorageStatus]:
        """Return the secret *and* how securely its location keeps it.

        A store may hold values in more than one place -- for example a secure
        backend plus a legacy plaintext file -- so the quality of the location
        a value actually came from is reported here rather than guessed from
        the store as a whole.
        """

        return self.get(key), self.storage_status

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


#: Sentinel meaning "discover the keyring module". ``None`` means "no keyring",
#: which lets a test exercise the unavailable path without uninstalling it.
_AUTO_KEYRING = object()


def _usable_keyring(module: Any) -> Any | None:
    """Return ``module`` only when its active backend can actually work.

    An installed ``keyring`` package is not proof of a functioning backend: on
    a headless Linux host it may select ``fail.Keyring`` or a null backend, and
    treating that as secure would advertise storage the application cannot
    use. The check is deliberately static -- probing with a write would create
    junk credentials and may prompt the user.
    """

    if module is None:
        return None
    try:
        backend = module.get_keyring()
    except Exception:
        logger.info("keyring has no usable backend; secure storage is unavailable")
        return None
    if backend is None:
        return None
    try:
        from keyring.backends import fail, null
    except Exception:  # pragma: no cover - very old keyring layout
        fail = null = ()  # type: ignore[assignment]
    if isinstance(backend, (fail.Keyring, null.Keyring)):
        return None
    try:
        priority = float(getattr(backend, "priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0.0
    if priority <= 0:
        return None
    return module


class KeyringSecretStore(SecretStore):
    """OS credential-manager backed store (Windows Credential Manager).

    The file store is still readable as a fallback so credentials written by
    earlier versions remain usable, and that read is reported as degraded.
    Writes never fall back: if the OS credential store cannot be reached, the
    operation fails visibly instead of putting the key in a plaintext file.
    """

    def __init__(
        self,
        fallback: SecretStore,
        username: str = "default",
        *,
        keyring_module: Any = _AUTO_KEYRING,
    ) -> None:
        self._fallback = fallback
        self._username = username
        self._keyring_installed = False
        if keyring_module is _AUTO_KEYRING:
            try:
                import keyring as keyring_module
            except Exception:
                logger.info("keyring is unavailable; secure storage is unavailable")
                self._keyring = None
            else:
                self._keyring_installed = True
                self._keyring = _usable_keyring(keyring_module)
        else:
            self._keyring_installed = keyring_module is not None
            self._keyring = _usable_keyring(keyring_module)
        if self._keyring is None and self._keyring_installed:
            logger.warning("keyring is installed but has no usable backend")

    @property
    def is_secure(self) -> bool:
        """Secure only when the OS credential manager is actually usable."""

        return self._keyring is not None

    @property
    def storage_status(self) -> StorageStatus:
        if self._keyring is not None:
            return StorageStatus.SECURE
        if self._keyring_installed:
            return StorageStatus.DEGRADED
        return StorageStatus.PLAINTEXT

    def _name(self, key: str) -> str:
        return f"{self._username}/{key}"

    def _keyring_value(self, key: str) -> SecretStr | None:
        if self._keyring is None:
            return None
        try:
            value = self._keyring.get_password(_KEYRING_SERVICE, self._name(key))
        except Exception as exc:
            raise SecretStoreError(
                f"the OS credential store could not be read ({type(exc).__name__})",
                user_message=(
                    "The OS credential store could not be read. Unlock it or check "
                    "the platform credential service and try again."
                ),
                details={"operation": "read"},
            ) from exc
        return SecretStr(value) if value else None

    def read(self, key: str) -> tuple[SecretStr | None, StorageStatus]:
        if self._keyring is not None:
            value = self._keyring_value(key)
            if value is not None:
                return value, StorageStatus.SECURE
            # A credential written before the OS store was adopted is still
            # readable, but it is reported as what it is, not as secure.
            legacy = self._fallback.get(key)
            if legacy is not None:
                return legacy, StorageStatus.PLAINTEXT
            return None, StorageStatus.SECURE
        # No usable secure backend: read the legacy plaintext half so existing
        # credentials are preserved, and report the degraded storage.
        return self._fallback.get(key), self.storage_status

    def get(self, key: str) -> SecretStr | None:
        return self.read(key)[0]

    def set(self, key: str, value: SecretStr) -> None:
        if self._keyring is None:
            raise SecretStoreError(
                "no usable OS credential store is available",
                user_message=(
                    "This system has no usable OS credential store, so the API key was "
                    "not saved. Nothing was written to disk."
                ),
                details={"operation": "write"},
            )
        try:
            self._keyring.set_password(_KEYRING_SERVICE, self._name(key), value.get_secret_value())
        except Exception as exc:
            raise SecretStoreError(
                f"the OS credential store refused the write ({type(exc).__name__})",
                user_message=(
                    "The OS credential store refused to save the API key. Nothing was "
                    "written to disk."
                ),
                details={"operation": "write"},
            ) from exc

    def delete(self, key: str) -> bool:
        if self._keyring is None:
            return self._fallback.delete(key)
        removed = False
        try:
            self._keyring.delete_password(_KEYRING_SERVICE, self._name(key))
            removed = True
        except Exception as exc:
            # A missing entry is not a failure to remove anything; every other
            # backend error is surfaced.
            if type(exc).__name__ != "PasswordDeleteError":
                raise SecretStoreError(
                    f"the OS credential store could not be updated ({type(exc).__name__})",
                    user_message="The OS credential store could not remove the API key.",
                    details={"operation": "delete"},
                ) from exc
        # An explicit removal removes every copy, including a plaintext one
        # left behind by an earlier version.
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
