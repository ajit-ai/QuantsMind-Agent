"""The ChangeSet store: what survives a crash, and where it lives on disk.

QM-P09-CS-001 §6.5 fixes the layout down to the file names. This module is the
only place that knows it, so the rest of the engine asks for paths instead of
building strings, and a change of layout is one file rather than an audit.

Three properties here are load-bearing for the whole engine.

- **Durable before acknowledged.** A metadata write is temp + ``fsync`` +
  ``os.replace``; a journal append is append + ``fsync`` and is then read back
  from the file before it returns. A successful in-memory call is not evidence
  that anything reached the disk.
- **Fail closed on read.** A manifest that exists but cannot be parsed raises.
  Returning ``None`` would let the caller treat a corrupted transaction as
  "nothing happened" and start a new one over it.
- **Names are validated, not assumed.** A workspace key, a ChangeSet id or an
  operation id that is not exactly the §3/§6.2 shape is refused before it can
  become a path component, because ``../`` in an id is a write outside the store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantsmind_agent.persistence.atomic import atomic_write_bytes, read_json_object
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.services.changesets.lifecycle import UNSTABLE_STATES

__all__ = [
    "CROCKFORD_BASE32",
    "JOURNAL_EVENT_FIELDS",
    "MAX_INLINE_PRE_IMAGE_BYTES",
    "SCHEMA_VERSION",
    "TERMINAL_JOURNAL_EVENTS",
    "BlobIntegrityError",
    "BlobStore",
    "CanonicalJSONError",
    "ChangeSetPaths",
    "ChangeSetStore",
    "CorruptRecordError",
    "Journal",
    "LayoutError",
    "StoredChangeSet",
    "canonical_json",
    "content_digest",
    "digest_of",
    "new_changeset_id",
    "sha256_hex",
    "utc_now_stamp",
    "verify_digest",
    "workspace_key_for",
]

#: The manifest/approval/verification/journal record shape this build reads and
#: writes. §6.1: ``schema_version`` is required and is checked, not trusted.
SCHEMA_VERSION = 1

#: §3: the workspace key is the first 16 hex characters of the digest over the
#: sorted, normalized root set.
WORKSPACE_KEY_LENGTH = 16

#: §3: ``cs-`` + a 26-character Crockford Base32 ULID.
ULID_LENGTH = 26
CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Pre-images always live in the blob store. The per-operation copies under
#: ``ops/<op_id>/`` exist for human inspection (§6.5 "optional small
#: pre-image"), so they are only written when the content is small enough that
#: duplicating it is cheaper than sending a reviewer to a hash-named file.
MAX_INLINE_PRE_IMAGE_BYTES = 1024 * 1024

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_CHANGESET_ID_RE = re.compile(r"^cs-[0-9A-Z]{26}$")
_OP_ID_RE = re.compile(r"^op-\d{4}$")

#: The journal holds paths and hashes, never content or secrets (§11.1). A key
#: in this set is refused at append time rather than scrubbed afterwards: once a
#: secret is on disk in a file the recovery path reads, redaction is a promise
#: the engine cannot keep.
_FORBIDDEN_JOURNAL_KEYS = frozenset(
    {
        "content",
        "body",
        "data",
        "payload",
        "stdout",
        "stderr",
        "prompt",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "authorization",
        "env",
    }
)

#: §11.1, transcribed. Every record must carry its own identifiers plus the
#: fields the crash scan in §11.2 needs to classify it; a record that cannot be
#: attributed is worse than no record, because recovery would guess.
JOURNAL_EVENT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "apply_started": (
        "seq",
        "ts",
        "changeset_id",
        "revision",
        "digest",
        "workspace_key",
        "op_ids",
        "pid",
    ),
    "op_started": ("seq", "op_id", "type", "path", "destination_path", "base", "result"),
    "op_committed": ("seq", "op_id", "actual_sha256", "actual_exists", "ts"),
    "op_failed": ("seq", "op_id", "code", "message", "actual_sha256", "actual_exists"),
    "conflict": ("seq", "op_id", "code", "expected", "actual"),
    "apply_completed": ("seq", "ts"),
    "rollback_started": ("seq", "reason", "op_ids"),
    "rollback_op_started": ("seq", "op_id"),
    "rollback_op_committed": ("seq", "op_id", "actual_sha256", "actual_exists"),
    "rollback_op_failed": ("seq", "op_id", "code", "message"),
    "rollback_completed": ("seq", "ts"),
    "recovery_classified": ("seq", "decision", "classification"),
}

#: Terminal for the apply/rollback sequence. A journal whose last terminal
#: record is followed by no new intent is not rescanned (§11.1).
TERMINAL_JOURNAL_EVENTS: frozenset[str] = frozenset({"apply_completed", "rollback_completed"})

_OP_OUTCOME_EVENTS = frozenset({"op_committed", "op_failed"})


class StorageError(RuntimeError):
    """A persistence refusal or failure. ``reason_code`` is stable; ``message`` is not."""

    def __init__(self, reason_code: str, message: str, *, path: str = "") -> None:
        super().__init__(f"{reason_code}: {message}" + (f" [{path}]" if path else ""))
        self.reason_code = reason_code
        self.path = path


class LayoutError(StorageError):
    """An id, key or name that cannot be turned into a path safely."""

    def __init__(self, message: str, *, value: str = "") -> None:
        super().__init__("layout_invalid", message, path=value)


class CorruptRecordError(StorageError):
    """A record that exists but cannot be read. Never treated as absence."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__("record_corrupt", message, path=path)


class BlobIntegrityError(StorageError):
    """A blob whose bytes no longer hash to the name it is stored under."""

    def __init__(self, digest: str, message: str = "content does not match its address") -> None:
        super().__init__("blob_integrity", message, path=digest)


class CanonicalJSONError(ValueError):
    """Material that cannot be canonicalised, so no digest can be claimed."""


# -- digests -----------------------------------------------------------------
def _reject_non_canonical(value: Any, trail: str) -> None:
    if isinstance(value, float):
        raise CanonicalJSONError(f"{trail}: floats are not canonical (§7.1); use basis points or a string")
    if isinstance(value, (str, int, bool)) or value is None:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(f"{trail}: object keys must be strings, got {type(key).__name__}")
            _reject_non_canonical(item, f"{trail}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_canonical(item, f"{trail}[{index}]")
        return
    raise CanonicalJSONError(f"{trail}: {type(value).__name__} is not JSON material")


def canonical_json(value: Any) -> bytes:
    """UTF-8, keys sorted, ``(",", ":")`` separators, no insignificant space (§7.1).

    An approval digest has to be reproducible from the persisted manifest alone,
    so the serialisation is part of the contract: two writers that disagree about
    spacing produce different digests and every approval looks invalid.
    """

    _reject_non_canonical(value, "$")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(data: bytes) -> str:
    """``sha256:<hex>`` over raw bytes -- the blob address (§6.5)."""

    return f"sha256:{sha256_hex(data)}"


def digest_of(material: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over canonical JSON -- the approval digest (§7.1)."""

    return f"sha256:{sha256_hex(canonical_json(material))}"


def verify_digest(digest: str, material: Mapping[str, Any]) -> bool:
    """Recompute and compare. A malformed stored digest simply does not match."""

    if not _DIGEST_RE.fullmatch(digest or ""):
        return False
    return digest == digest_of(material)


def utc_now_stamp() -> str:
    """ISO-8601 UTC with a fixed ``Z`` suffix, as §6.1 requires of timestamps."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


# -- identity ----------------------------------------------------------------
def normalize_root_path(root: str | os.PathLike[str]) -> str:
    """One spelling of one root: forward slashes, no trailing separator, casefolded."""

    text = os.fspath(root).replace("\\", "/")
    stripped = text.rstrip("/")
    return os.path.normcase(stripped or "/")


def workspace_key_for(roots: Iterable[str | os.PathLike[str]]) -> str:
    """First 16 hex of SHA-256 over the sorted, normalized root set (§3).

    A key identifies a root *set*, not a machine: moving the workspace yields a
    different key, and a ChangeSet created for the old key must be refused. That
    refusal depends on this function being the only way keys are made.
    """

    normalized = sorted({normalize_root_path(root) for root in roots})
    if not normalized:
        raise LayoutError("a workspace key needs at least one root")
    material = canonical_json(normalized)
    return sha256_hex(material)[:WORKSPACE_KEY_LENGTH]


def _encode_base32(value: int, digits: int) -> str:
    characters = [""] * digits
    for position in range(digits - 1, -1, -1):
        characters[position] = CROCKFORD_BASE32[value & 0x1F]
        value >>= 5
    if value:
        raise OverflowError("value does not fit the requested base32 width")
    return "".join(characters)


def new_changeset_id(now: float | None = None) -> str:
    """``cs-`` + a fresh ULID: 48 bits of millisecond timestamp, 80 random (§3)."""

    milliseconds = int((time.time() if now is None else now) * 1000) & ((1 << 48) - 1)
    value = (milliseconds << 80) | secrets.randbits(80)
    return f"cs-{_encode_base32(value, ULID_LENGTH)}"


def _checked_id(value: str, *, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value or ""):
        raise LayoutError(
            f"{label} {value!r} is not the accepted shape; a path component must not be free-form",
            value=value,
        )
    return value


# -- blobs -------------------------------------------------------------------
class BlobStore:
    """Content-addressed storage at ``<data>/blobs/sha256/<aa>/<hash>`` (§6.5).

    Writes are idempotent by hash (§11.4): staging the same bytes twice is one
    file, and a re-run of a crashed recovery never duplicates a pre-image.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, digest: str) -> Path:
        if not _DIGEST_RE.fullmatch(digest or ""):
            raise LayoutError(f"{digest!r} is not a sha256:<hex> digest", value=digest)
        hex64 = digest.split(":", 1)[1]
        return self._root / hex64[:2] / hex64

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put(self, data: bytes) -> str:
        """Store ``data`` and return its digest. Content is never trusted from memory."""

        digest = content_digest(data)
        path = self.path_for(digest)
        if path.is_file():
            return digest
        atomic_write_bytes(path, data)
        # Read it back: the name is a claim about the bytes, and an unverified
        # claim is how a rollback later restores the wrong file.
        if path.read_bytes() != data:
            raise BlobIntegrityError(digest, "written bytes did not read back identically")
        return digest

    def read(self, digest: str) -> bytes:
        path = self.path_for(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobIntegrityError(digest, "the pre-image is not on disk") from exc
        except OSError as exc:
            raise BlobIntegrityError(digest, f"unreadable: {exc}") from exc
        if content_digest(data) != digest:
            raise BlobIntegrityError(digest)
        return data

    def size(self, digest: str) -> int:
        return self.path_for(digest).stat().st_size


# -- layout ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChangeSetPaths:
    """Every file belonging to one ChangeSet, under ``<changesets>/<key>/<id>``."""

    store_root: Path
    workspace_key: str
    changeset_id: str

    def __post_init__(self) -> None:
        _checked_id(self.workspace_key, pattern=_WORKSPACE_KEY_RE, label="workspace key")
        _checked_id(self.changeset_id, pattern=_CHANGESET_ID_RE, label="changeset id")

    @property
    def root(self) -> Path:
        return Path(self.store_root) / self.workspace_key / self.changeset_id

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def approval(self) -> Path:
        return self.root / "approval.json"

    @property
    def verification(self) -> Path:
        return self.root / "verification.json"

    @property
    def journal(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def diff_patch(self) -> Path:
        return self.root / "diff.patch"

    @property
    def ops_dir(self) -> Path:
        return self.root / "ops"

    def op_dir(self, op_id: str) -> Path:
        _checked_id(op_id, pattern=_OP_ID_RE, label="operation id")
        return self.ops_dir / op_id

    def op_file(self, op_id: str, name: str) -> Path:
        if name not in {"base.bin", "result.bin"}:
            raise LayoutError(f"{name!r} is not a per-operation file §6.5 defines", value=name)
        return self.op_dir(op_id) / name


# -- journal -----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class JournalScan:
    """What the journal says, including the part of it the crash left unfinished."""

    records: tuple[dict[str, Any], ...]
    torn_tail: str = ""

    @property
    def last_seq(self) -> int:
        return self.records[-1]["seq"] if self.records else 0

    def last_terminal_index(self) -> int:
        for index in range(len(self.records) - 1, -1, -1):
            if self.records[index]["event"] in TERMINAL_JOURNAL_EVENTS:
                return index
        return -1

    def needs_scan(self) -> bool:
        """True when an intent is open, or the tail is a half-written record.

        Terminal followed by nothing is the clean case §11.1 says not to
        rescan. Anything after a terminal -- a new intent -- opens it again.
        """

        if self.torn_tail:
            return True
        return self.last_terminal_index() < len(self.records) - 1

    def committed_op_ids(self) -> frozenset[str]:
        return frozenset(
            str(record["op_id"])
            for record in self.records
            if record["event"] in ("op_committed", "rollback_op_committed")
        )

    def started_op_ids(self) -> tuple[str, ...]:
        return tuple(
            str(record["op_id"])
            for record in self.records
            if record["event"] in ("op_started", "rollback_op_started")
        )

    def unresolved_op_ids(self) -> tuple[str, ...]:
        """Started but never given an outcome -- the §11.2 "op_started only" row."""

        resolved = self.committed_op_ids() | {
            str(record["op_id"]) for record in self.records if record["event"] == "op_failed"
        }
        return tuple(op_id for op_id in self.started_op_ids() if op_id not in resolved)

    def op_record(self, op_id: str, event: str) -> dict[str, Any] | None:
        for record in reversed(self.records):
            if record["event"] == event and record.get("op_id") == op_id:
                return record
        return None


class Journal:
    """``journal.jsonl``: append-only, one JSON object per line, fsync'd per record."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _required(self, event: str) -> tuple[str, ...]:
        try:
            return JOURNAL_EVENT_FIELDS[event]
        except KeyError as exc:
            raise StorageError(
                "journal_event_unknown",
                f"{event!r} is not a journal event §11.1 defines",
                path=str(self._path),
            ) from exc

    def scan(self) -> JournalScan:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return JournalScan(records=())
        except OSError as exc:
            raise CorruptRecordError(f"journal unreadable: {exc}", path=str(self._path)) from exc
        if not raw:
            return JournalScan(records=())
        records: list[dict[str, Any]] = []
        previous_seq = 0
        for line in raw.splitlines(keepends=True):
            text = line.decode("utf-8", errors="replace").strip()
            terminated = line.endswith(b"\n")
            if not text:
                continue
            if not terminated:
                return JournalScan(records=tuple(records), torn_tail=text)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CorruptRecordError(
                    f"journal line {len(records) + 1} is not JSON: {exc}", path=str(self._path)
                ) from exc
            if not isinstance(parsed, dict):
                raise CorruptRecordError(
                    f"journal line {len(records) + 1} is not an object", path=str(self._path)
                )
            seq = parsed.get("seq")
            if not isinstance(seq, int) or seq <= previous_seq:
                raise CorruptRecordError(
                    f"journal line {len(records) + 1} has seq {seq!r} after {previous_seq}",
                    path=str(self._path),
                )
            previous_seq = seq
            records.append(parsed)
        return JournalScan(records=tuple(records))

    def next_seq(self) -> int:
        return self.scan().last_seq + 1

    def append(self, event: str, fields: Mapping[str, Any] | None = None, **extra: Any) -> int:
        """Write one record and prove it is on disk. Returns its sequence number."""

        required = self._required(event)
        record: dict[str, Any] = dict(fields or {})
        record.update(extra)
        record["event"] = event

        offending = _FORBIDDEN_JOURNAL_KEYS.intersection(key.lower() for key in record)
        if offending:
            raise StorageError(
                "journal_field_forbidden",
                f"{event} carries {sorted(offending)}; the journal holds paths and hashes only (§11.1)",
                path=str(self._path),
            )

        # ``seq`` and ``ts`` are always assignable by the writer: seq is a serial
        # number over the append-only log, and ts is filled in now for every event
        # that §11.1 requires it. A caller never has to hand them to `append`,
        # so they are not part of the "missing" refusal set.
        #
        # A field that is present but None is still present; the tests and the
        # journal contract use null for optional destinations and left-to-right
        # ordering, so null is a valid value and must never be treated as absent.
        missing = [name for name in required if name not in record]
        if missing:
            not_auto = [name for name in missing if name not in {"seq", "ts"}]
            if not_auto:
                raise StorageError(
                    "journal_field_missing",
                    f"{event} is missing {not_auto}; a record recovery cannot attribute is a record worth nothing",
                    path=str(self._path),
                )

        if "seq" not in record or record["seq"] is None:
            record["seq"] = self.next_seq()
        if "ts" in required and not record.get("ts"):
            record["ts"] = utc_now_stamp()

        line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        encoded = f"{line}\n".encode()

        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise StorageError(
                    "journal_short_write",
                    f"wrote {written} of {len(encoded)} bytes",
                    path=str(self._path),
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        # Durability is verified, not assumed: the record is read back from the
        # file before the caller is told the intent landed.
        tail = self.scan()
        if not tail.records or tail.records[-1].get("seq") != record["seq"]:
            raise StorageError(
                "journal_not_durable",
                f"seq {record['seq']} is not the last record on disk",
                path=str(self._path),
            )
        return int(record["seq"])


# -- store -------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredChangeSet:
    """One directory and whatever it holds -- including nothing readable."""

    paths: ChangeSetPaths
    manifest: dict[str, Any] | None
    error: str = ""

    @property
    def status(self) -> str:
        return str((self.manifest or {}).get("status", ""))


class ChangeSetStore:
    """The four declared directories of §6.5, and the only API over them.

    Nothing here caches. Every read goes to the file, because §5 says a method
    "never trusts an in-memory copy" and an unstable transaction is exactly the
    moment a stale copy would do damage.
    """

    def __init__(
        self,
        *,
        changesets_dir: Path,
        blobs_dir: Path,
        locks_dir: Path,
        quarantine_dir: Path,
    ) -> None:
        self._changesets_dir = Path(changesets_dir)
        self._blobs_dir = Path(blobs_dir)
        self._locks_dir = Path(locks_dir)
        self._quarantine_dir = Path(quarantine_dir)
        self._blobs = BlobStore(self._blobs_dir)

    @classmethod
    def from_app_paths(cls, app_paths: AppPaths) -> ChangeSetStore:
        """Bind to ``AppPaths`` -- the declared locations, not invented ones."""

        return cls(
            changesets_dir=Path(app_paths.changesets_dir),
            blobs_dir=Path(app_paths.blobs_dir),
            locks_dir=Path(app_paths.locks_dir),
            quarantine_dir=Path(app_paths.quarantine_dir),
        )

    @property
    def changesets_dir(self) -> Path:
        return self._changesets_dir

    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    @property
    def locks_dir(self) -> Path:
        return self._locks_dir

    @property
    def quarantine_dir(self) -> Path:
        return self._quarantine_dir

    def ensure(self) -> ChangeSetStore:
        for directory in (
            self._changesets_dir,
            self._blobs_dir,
            self._locks_dir,
            self._quarantine_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def lock_path(self, workspace_key: str) -> Path:
        """``<locks>/<workspace_key>.apply.lock`` -- the single-applier lease (§6.5)."""

        key = _checked_id(workspace_key, pattern=_WORKSPACE_KEY_RE, label="workspace key")
        return self._locks_dir / f"{key}.apply.lock"

    def paths_for(self, workspace_key: str, changeset_id: str) -> ChangeSetPaths:
        return ChangeSetPaths(
            store_root=self._changesets_dir,
            workspace_key=workspace_key,
            changeset_id=changeset_id,
        )

    def quarantine_dir_for(self, workspace_key: str, changeset_id: str, op_id: str) -> Path:
        """``<data>/recovery/quarantine/<key>/<id>/<op_id>`` (§6.5)."""

        key = _checked_id(workspace_key, pattern=_WORKSPACE_KEY_RE, label="workspace key")
        ident = _checked_id(changeset_id, pattern=_CHANGESET_ID_RE, label="changeset id")
        operation = _checked_id(op_id, pattern=_OP_ID_RE, label="operation id")
        return self._quarantine_dir / key / ident / operation

    def journal(self, paths: ChangeSetPaths) -> Journal:
        return Journal(paths.journal)

    # -- json metadata
    @staticmethod
    def write_json(path: Path, payload: Mapping[str, Any]) -> None:
        """temp + fsync + ``os.replace`` (§6.5), then verified from the file."""

        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        atomic_write_bytes(Path(path), f"{text}\n".encode())
        if read_json_object(Path(path)) is None:
            raise CorruptRecordError("the metadata just written cannot be read back", path=str(path))

    @staticmethod
    def read_json(path: Path) -> dict[str, Any] | None:
        path = Path(path)
        if not path.exists():
            return None
        payload = read_json_object(path)
        if payload is None:
            raise CorruptRecordError(
                f"{path.name} exists but is not a JSON object; the transaction it belongs to is unresolved",
                path=str(path),
            )
        return payload

    def save_manifest(self, paths: ChangeSetPaths, manifest: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(manifest)
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise StorageError(
                "manifest_schema_unsupported",
                f"schema_version {version!r}, this build writes {SCHEMA_VERSION}",
                path=str(paths.manifest),
            )
        for field in ("changeset_id", "workspace_key", "status"):
            if payload.get(field) in (None, ""):
                raise StorageError(
                    "manifest_field_missing",
                    f"{field} is required by §6.1",
                    path=str(paths.manifest),
                )
        if payload["changeset_id"] != paths.changeset_id or payload["workspace_key"] != paths.workspace_key:
            raise StorageError(
                "manifest_identity_mismatch",
                "the manifest does not belong in the directory it is being written to",
                path=str(paths.manifest),
            )
        payload.setdefault("created_at", utc_now_stamp())
        payload["updated_at"] = utc_now_stamp()
        self.write_json(paths.manifest, payload)
        return payload

    def load_manifest(self, paths: ChangeSetPaths) -> dict[str, Any] | None:
        payload = self.read_json(paths.manifest)
        if payload is None:
            return None
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise StorageError(
                "manifest_schema_unsupported",
                f"a future or older schema cannot be interpreted here: {version!r}",
                path=str(paths.manifest),
            )
        # An id that disagrees with its directory means the store was rearranged
        # by hand or a key was recomputed; either way the evidence is no longer
        # attributable, and reading on would act on the wrong transaction.
        if payload.get("changeset_id") != paths.changeset_id or payload.get("workspace_key") != paths.workspace_key:
            raise CorruptRecordError(
                "manifest identity does not match its location", path=str(paths.manifest)
            )
        return payload

    def save_approval(self, paths: ChangeSetPaths, approval: Mapping[str, Any]) -> None:
        self.write_json(paths.approval, {**dict(approval), "schema_version": SCHEMA_VERSION})

    def load_approval(self, paths: ChangeSetPaths) -> dict[str, Any] | None:
        return self.read_json(paths.approval)

    def save_verification(self, paths: ChangeSetPaths, verification: Mapping[str, Any]) -> None:
        self.write_json(
            paths.verification, {**dict(verification), "schema_version": SCHEMA_VERSION}
        )

    def load_verification(self, paths: ChangeSetPaths) -> dict[str, Any] | None:
        return self.read_json(paths.verification)

    def save_diff(self, paths: ChangeSetPaths, patch: str) -> str:
        """Render the convenience diff and return its digest (§6.1 ``diff.sha256``)."""

        data = patch.encode("utf-8")
        atomic_write_bytes(paths.diff_patch, data)
        return content_digest(data)

    # -- staged content
    def stage(
        self, paths: ChangeSetPaths, op_id: str, data: bytes, *, kind: str
    ) -> tuple[str, bool]:
        """Store ``data`` as a blob and, when small, keep a per-operation copy.

        Returns ``(digest, inline_written)``. The blob is authoritative; the
        ``ops/<op_id>/<kind>.bin`` copy is what a reviewer or a recovery operator
        opens when they want to see a pre-image without a hex editor and a hash.
        """

        digest = self._blobs.put(data)
        inline = len(data) <= MAX_INLINE_PRE_IMAGE_BYTES
        if inline:
            atomic_write_bytes(paths.op_file(op_id, f"{kind}.bin"), data)
        return digest, inline

    def read_staged(self, digest: str) -> bytes:
        return self._blobs.read(digest)

    # -- enumeration
    def list_workspace_keys(self) -> tuple[str, ...]:
        if not self._changesets_dir.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self._changesets_dir.iterdir()
                if entry.is_dir() and _WORKSPACE_KEY_RE.fullmatch(entry.name)
            )
        )

    def stored(self, workspace_key: str, changeset_id: str) -> StoredChangeSet:
        paths = self.paths_for(workspace_key, changeset_id)
        if not paths.manifest.exists():
            return StoredChangeSet(paths=paths, manifest=None)
        try:
            return StoredChangeSet(paths=paths, manifest=self.load_manifest(paths))
        except (StorageError, CorruptRecordError) as exc:
            return StoredChangeSet(paths=paths, manifest=None, error=str(exc))

    def iter_stored(self, workspace_key: str | None = None) -> tuple[StoredChangeSet, ...]:
        keys = (workspace_key,) if workspace_key else self.list_workspace_keys()
        found: list[StoredChangeSet] = []
        for key in keys:
            _checked_id(key or "", pattern=_WORKSPACE_KEY_RE, label="workspace key")
            directory = self._changesets_dir / key
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir() or not _CHANGESET_ID_RE.fullmatch(entry.name):
                    continue
                found.append(self.stored(key, entry.name))
        return tuple(found)

    def needing_scan(self, workspace_key: str | None = None) -> tuple[tuple[StoredChangeSet, str], ...]:
        """§11.2: unstable by manifest status, or a journal whose intent is open.

        Both are reported even when the other disagrees. A manifest that says
        ``APPLIED`` next to an open journal is exactly the lie recovery must not
        pick a side in; the caller gets the pair and classifies it.
        """

        results: list[tuple[StoredChangeSet, str]] = []
        for record in self.iter_stored(workspace_key):
            reasons: list[str] = []
            if record.error:
                reasons.append("manifest_unreadable")
            elif record.status in UNSTABLE_STATES:
                reasons.append(f"status:{record.status}")
            if record.paths.journal.exists():
                try:
                    if self.journal(record.paths).scan().needs_scan():
                        reasons.append("journal_open")
                except CorruptRecordError as exc:
                    reasons.append(f"journal_corrupt:{exc.reason_code}")
            if reasons:
                results.append((record, ";".join(reasons)))
        return tuple(results)
