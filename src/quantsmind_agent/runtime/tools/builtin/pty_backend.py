"""Cross-platform pseudo-terminal backend for interactive command execution.

Windows uses the ConPTY API (``CreatePseudoConsole`` / ``ResizePseudoConsole`` /
``ClosePseudoConsole``) exposed through ``ctypes``. Linux and macOS use the
stdlib ``pty`` module. The backend starts a program inside a pseudo-terminal so
that interactive programs (REPLs, prompts, screen-oriented tools) see a real
terminal rather than a pipe.

Input is written to the PTY master; output is read from the PTY master. The
backend is bounded: output is capped, the runtime is capped, and cancellation
terminates the process and its process group.

Nothing here imports Qt; the backend runs on a worker thread, never on the GUI
thread.
"""

from __future__ import annotations

import contextlib
import os
import select
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from quantsmind_agent.errors import CommandExecutionError

__all__ = [
    "PtyBackend",
    "PtyOutput",
    "PtySession",
    "PtyUnavailable",
    "spawn_shell",
]

#: Upper bounds, so a misbehaving terminal cannot exhaust memory or wedge the GUI.
MAX_OUTPUT_CHARS = 50_000
MAX_TIMEOUT_SECONDS = 600.0
MIN_TIMEOUT_SECONDS = 1.0
#: How often the read loop wakes to check the cancel flag and exit status.
_POLL_SECONDS = 0.05
#: Grace period between asking a process to stop and killing it.
_TERMINATE_GRACE_SECONDS = 3.0

#: Environment variables removed from every child process.
_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "QM_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_ORGANIZATION",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


@dataclass(slots=True)
class PtyOutput:
    """One chunk of terminal output."""

    text: str
    """Decoded text (may be partial UTF-8 at chunk boundaries)."""

    exited: bool
    """Whether the process has exited (final chunk may carry exit info)."""

    exit_code: int | None
    """Process exit code when ``exited`` is true, else ``None``."""


class PtyUnavailable(CommandExecutionError):
    """Raised when a pseudo-terminal cannot be created on this host."""

    code = "QM_PTY_UNAVAILABLE"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "interactive terminal",
            reason,
            details={"code": "pty_unavailable", "reason": reason},
        )


class PtySession:
    """One live pseudo-terminal session.

    Owns the PTY master file descriptors and the child process. Reads output
    from the master, writes input to the master, resizes the terminal, and
    terminates the process group on cancellation or timeout.

    Parameters
    ----------
    read_fd:
        OS file descriptor used to read terminal output from the master.
    write_fd:
        OS file descriptor used to write terminal input to the master.
    process_handle:
        Platform-native process handle (int on Windows, unused on POSIX).
    pid:
        The child process id.
    columns, rows:
        Initial terminal dimensions in characters.
    cancel_event:
        Optional :class:`threading.Event`; when set, the session is terminated.
    timeout_seconds:
        Hard runtime cap, clamped to ``[MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]``.
    """

    def __init__(
        self,
        *,
        read_fd: int,
        write_fd: int,
        process_handle: Any,
        pid: int,
        columns: int,
        rows: int,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float = 120.0,
        conpty_handle: Any = None,
    ) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._process_handle = process_handle
        self._pid = pid
        self._columns = columns
        self._rows = rows
        self._cancel = cancel_event
        self._timeout_seconds = min(max(timeout_seconds, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)
        self._deadline = time.perf_counter() + self._timeout_seconds
        self._exit_code: int | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._fds_closed = False
        self._output_chars = 0
        self._omitted_chars = 0
        self._truncated = False
        #: Retained ConPTY handle (Windows) so resize/close can use it.
        self._hpc = conpty_handle
        #: Windows streaming queue fed by a dedicated reader thread. Unlike
        #: POSIX fds, Windows pipe handles are not selectable, so read()
        #: drains this queue instead of polling the fd itself.
        self._win_queue: Any = None
        self._win_reader: threading.Thread | None = None

    # -- properties ---------------------------------------------------------
    @property
    def pid(self) -> int:
        return self._pid

    @property
    def exit_code(self) -> int | None:
        with self._lock:
            if self._exit_code is None:
                self._exit_code = self._get_exit_code()
            return self._exit_code

    @property
    def is_alive(self) -> bool:
        return self._get_exit_code() is None

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def omitted_chars(self) -> int:
        return self._omitted_chars

    def start_windows_reader(self, pump: Any = None) -> None:
        """Start the Windows output pump (called by the spawners)."""
        import queue as _queue

        self._win_queue = _queue.Queue()
        target = pump if pump is not None else self._windows_pump
        self._win_reader = threading.Thread(target=target, daemon=True)
        self._win_reader.start()

    def _windows_pump(self) -> None:
        """Blocking reader: forwards PTY bytes to the queue as they arrive."""
        while True:
            try:
                data = os.read(self._read_fd, 4096)
            except OSError:
                break
            if not data:
                break
            if self._win_queue is not None:
                self._win_queue.put(data.decode("utf-8", errors="replace"))
        if self._win_queue is not None:
            self._win_queue.put(None)  # EOF sentinel

    def _pipe_pump(self, process: Any) -> None:
        """Pump for the pipe fallback: reads the child's binary stdout."""
        stream = getattr(getattr(process, "stdout", None), "buffer", None)
        if stream is None:
            if self._win_queue is not None:
                self._win_queue.put(None)
            return
        while True:
            try:
                data = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            except (OSError, ValueError):
                break
            if not data:
                break
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            if self._win_queue is not None:
                self._win_queue.put(data)
        if self._win_queue is not None:
            self._win_queue.put(None)

    # -- resize -------------------------------------------------------------
    def resize(self, columns: int, rows: int) -> None:
        """Change terminal dimensions. A no-op if dimensions are unchanged."""

        if columns <= 0 or rows <= 0:
            return
        if columns == self._columns and rows == self._rows:
            return
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                _win32_resize_pty(self, columns, rows)
        self._columns = columns
        self._rows = rows

    # -- input / output -----------------------------------------------------
    def write(self, data: bytes) -> None:
        """Send ``data`` to the terminal as if typed by the user."""

        if self._closed:
            return
        os.write(self._write_fd, data)

    def read(self, timeout: float = _POLL_SECONDS) -> PtyOutput | None:
        """Read one chunk of terminal output.

        Returns ``None`` when no output is available within ``timeout`` and the
        process is still running. Returns a final chunk with ``exited=True``
        once the process has exited and the PTY is drained.
        """

        # Windows: drain the streaming queue so interactive output arrives
        # without waiting for process exit.
        if sys.platform == "win32" and self._win_queue is not None:
            return self._read_windows(timeout)

        while True:
            if self._closed:
                return None
            exit_code = self._get_exit_code()
            if exit_code is not None:
                remaining = self._drain()
                return PtyOutput(text=remaining, exited=True, exit_code=exit_code)
            if self._cancel is not None and self._cancel.is_set():
                self.terminate()
                return PtyOutput(text="", exited=True, exit_code=self._get_exit_code())
            if time.perf_counter() >= self._deadline:
                self.terminate()
                exit_code = self._get_exit_code()
                return PtyOutput(text="", exited=True, exit_code=exit_code)
            ready = self._wait_readable(timeout)
            if not ready:
                return None
            chunk = self._read_available()
            if chunk:
                return PtyOutput(text=chunk, exited=False, exit_code=None)
            # Spurious wake: loop and re-check exit.

    def _read_windows(self, timeout: float) -> PtyOutput | None:
        import queue as _queue

        if self._closed:
            return None
        if self._cancel is not None and self._cancel.is_set():
            self.terminate()
            return PtyOutput(text="", exited=True, exit_code=self._get_exit_code())
        if time.perf_counter() >= self._deadline:
            self.terminate()
            return PtyOutput(text="", exited=True, exit_code=self._get_exit_code())
        try:
            item = self._win_queue.get(timeout=max(timeout, 0.0))
        except _queue.Empty:
            exit_code = self._get_exit_code()
            if exit_code is not None:
                return PtyOutput(text="", exited=True, exit_code=exit_code)
            return None
        if item is None:
            # EOF: process exited and pump drained. The process may still
            # need a moment to be reaped, so poll briefly for its code.
            exit_code = None
            for _ in range(40):
                exit_code = self._get_exit_code()
                if exit_code is not None:
                    break
                time.sleep(0.05)
            return PtyOutput(text="", exited=True, exit_code=exit_code)
        text = self._account_output(item)
        return PtyOutput(text=text, exited=False, exit_code=None)

    def _account_output(self, text: str) -> str:
        """Apply the output cap to one streamed chunk."""
        self._output_chars += len(text)
        if self._output_chars > MAX_OUTPUT_CHARS:
            self._truncated = True
            self._omitted_chars = self._output_chars - MAX_OUTPUT_CHARS
            head = text[:MAX_OUTPUT_CHARS]
            return head + f"\r\n[... {self._omitted_chars} chars omitted ...]\r\n"
        return text

    # -- lifecycle ----------------------------------------------------------
    def terminate(self) -> None:
        """Terminate the process and close the PTY master fds exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._process_handle is not None and self._get_exit_code() is None:
                _terminate_process_group(self._process_handle, self._pid)
                if sys.platform == "win32":
                    _wait_process(self._process_handle, _TERMINATE_GRACE_SECONDS)
                else:
                    with contextlib.suppress(Exception):
                        self._process_handle.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except Exception:  # pragma: no cover - defensive
            pass
        finally:
            self._close_fds()

    def _close_fds(self) -> None:
        with self._lock:
            if self._fds_closed:
                return
            self._fds_closed = True
        # Close the retained ConPTY handle first so the pump sees EOF and
        # exits instead of blocking forever on the output pipe.
        if self._hpc is not None:
            with contextlib.suppress(Exception):
                _close_conpty(self._hpc)
            self._hpc = None
        # Pipe-fallback sessions (read_fd==-1) pass fd ownership to the
        # child Popen/TextIOWrapper; closing the raw fd here would cause
        # Bad file descriptor on their finalization. Only close fds we own
        # (ConPTY open_osfhandle duplicates).
        if self._read_fd == -1:
            handle = self._process_handle
            if hasattr(handle, "stdin"):
                with contextlib.suppress(Exception):
                    handle.stdin.close()
            return
        for fd in (self._read_fd, self._write_fd):
            if fd is None or fd < 0:
                continue
            with contextlib.suppress(OSError):
                os.close(fd)

    # -- internals ----------------------------------------------------------
    def _get_exit_code(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        if sys.platform == "win32" and not isinstance(self._process_handle, subprocess.Popen):
            code = _get_exit_code(self._process_handle, self._pid)
        else:
            code = self._process_handle.poll()
        if code is not None:
            with self._lock:
                if self._exit_code is None:
                    self._exit_code = code
            return code
        return None

    def _wait_readable(self, timeout: float) -> bool:
        if sys.platform == "win32":
            # Windows PTY master fd is not selectable; poll with small sleeps.
            deadline = time.perf_counter() + max(timeout, 0.0)
            while time.perf_counter() < deadline:
                if self._closed:
                    return False
                if self._get_exit_code() is not None:
                    return True
                time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.perf_counter())))
            return False
        rlist, _, _ = select.select([self._read_fd], [], [], max(timeout, 0.0))  # type: ignore[unreachable]  # POSIX only; Windows branch always returns
        return bool(rlist)

    def _read_available(self) -> str:
        """Read whatever is immediately available, decoding UTF-8 incrementally."""

        chunks: list[str] = []
        while True:
            try:
                data = os.read(self._read_fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                # PTY closed (process exited).
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", errors="replace"))
        if not chunks:
            return ""
        text = "".join(chunks)
        self._output_chars += len(text)
        if self._output_chars > MAX_OUTPUT_CHARS:
            self._truncated = True
            self._omitted_chars = self._output_chars - MAX_OUTPUT_CHARS
            head = text[:MAX_OUTPUT_CHARS]
            return head + f"\r\n[... {self._omitted_chars} chars omitted ...]\r\n"
        return text

    def _drain(self) -> str:
        """Drain any remaining output after exit (non-blocking)."""

        out: list[str] = []
        while True:
            try:
                data = os.read(self._read_fd, 4096)
            except OSError:
                break
            if not data:
                break
            out.append(data.decode("utf-8", errors="replace"))
        return "".join(out)


class PtyBackend:
    """Creates pseudo-terminal sessions.

    On Windows this uses the ConPTY API. On Linux/macOS it uses
    :func:`pty.openpty`. Raises :class:`PtyUnavailable` if no PTY can be
    created.
    """

    def spawn(
        self,
        argv: list[str],
        *,
        cwd: str | os.PathLike[str],
        env: dict[str, str] | None = None,
        columns: int = 80,
        rows: int = 24,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float = 120.0,
    ) -> PtySession:
        """Start ``argv`` inside a pseudo-terminal at ``cwd``.

        The first element of ``argv`` is the program; the rest are arguments.
        A shell is *not* invoked unless the caller passes one explicitly
        (``["powershell.exe"]`` or ``["bash"]``).
        """

        if columns <= 0 or rows <= 0:
            columns, rows = 80, 24

        if sys.platform == "win32":
            return _win32_spawn(
                argv=argv,
                cwd=str(cwd),
                env=env,
                columns=columns,
                rows=rows,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
        return _posix_spawn(  # type: ignore[unreachable]  # POSIX only; Windows branch always returns
            argv=argv,
            cwd=str(cwd),
            env=env,
            columns=columns,
            rows=rows,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )


def spawn_shell(
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str] | None = None,
    columns: int = 80,
    rows: int = 24,
    cancel_event: threading.Event | None = None,
    timeout_seconds: float = 120.0,
) -> PtySession:
    """Convenience: start the user's default shell in a PTY at ``cwd``.

    On Windows this is ``powershell.exe``; elsewhere ``$SHELL`` (or ``sh``).
    """

    backend = PtyBackend()
    if sys.platform == "win32":
        shell = os.environ.get("COMSPEC", "powershell.exe")
        argv = [shell]
    else:
        shell = os.environ.get("SHELL", "sh")
        argv = [shell]
    return backend.spawn(
        argv=argv,
        cwd=cwd,
        env=env,
        columns=columns,
        rows=rows,
        cancel_event=cancel_event,
        timeout_seconds=timeout_seconds,
    )


# ============================================================================
# POSIX (Linux/macOS) implementation
# ============================================================================

if sys.platform != "win32":
    import pty
    import termios

    def _posix_spawn(
        *,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None,
        columns: int,
        rows: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float,
    ) -> PtySession:
        import fcntl

        master_fd, slave_fd = pty.openpty()
        try:
            # Set window size on the slave before exec.
            winsize = struct.pack("HHHH", rows, columns, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            # Non-fatal: dimensions are tracked in the session.
            pass

        env = dict(env or os.environ)
        # Strip credentials so a shell cannot leak them.
        for name in _CREDENTIAL_ENV_NAMES:
            env.pop(name, None)

        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)

        # Make the master non-blocking so reads poll instead of block.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        return PtySession(
            read_fd=master_fd,
            write_fd=master_fd,
            process_handle=process,
            pid=process.pid,
            columns=columns,
            rows=rows,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )

    def _close_conpty(hpc: Any) -> None:  # POSIX: no ConPTY handle.
        return None

    def _terminate_process_group(process_handle: Any, pid: int = 0) -> None:
        target = pid or getattr(process_handle, "pid", 0)
        if not target:
            return
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(target), 15)  # SIGTERM
        with contextlib.suppress(Exception):
            if process_handle is not None and hasattr(process_handle, "terminate"):
                process_handle.terminate()

    def _wait_process(process_handle: Any, timeout: float) -> None:
        with contextlib.suppress(Exception):
            process_handle.wait(timeout=timeout)

else:
    _posix_spawn = None


# ============================================================================
# Windows ConPTY implementation
# ============================================================================

if sys.platform == "win32":
    import ctypes.wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # ---- constants -------------------------------------------------------
    CREATE_UNICODE_ENVIRONMENT = 0x00001000
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x0002001C
    INFINITE = 0xFFFFFFFF
    HANDLE_FLAG_INHERIT = 0x00000001
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    STD_INPUT_HANDLE = -10

    # ---- structures ------------------------------------------------------
    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("lpReserved", ctypes.wintypes.LPWSTR),
            ("lpDesktop", ctypes.wintypes.LPWSTR),
            ("lpTitle", ctypes.wintypes.LPWSTR),
            ("dwX", ctypes.wintypes.DWORD),
            ("dwY", ctypes.wintypes.DWORD),
            ("dwXSize", ctypes.wintypes.DWORD),
            ("dwYSize", ctypes.wintypes.DWORD),
            ("dwXCountChars", ctypes.wintypes.DWORD),
            ("dwYCountChars", ctypes.wintypes.DWORD),
            ("dwFillAttribute", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("wShowWindow", ctypes.wintypes.WORD),
            ("cbReserved2", ctypes.wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", ctypes.wintypes.HANDLE),
            ("hStdOutput", ctypes.wintypes.HANDLE),
            ("hStdError", ctypes.wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.wintypes.HANDLE),
            ("hThread", ctypes.wintypes.HANDLE),
            ("dwProcessId", ctypes.wintypes.DWORD),
            ("dwThreadId", ctypes.wintypes.DWORD),
        ]

    # ---- function prototypes --------------------------------------------
    _CreatePseudoConsole = kernel32.CreatePseudoConsole
    _CreatePseudoConsole.argtypes = [
        COORD,
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _CreatePseudoConsole.restype = ctypes.c_long

    _ResizePseudoConsole = kernel32.ResizePseudoConsole
    _ResizePseudoConsole.argtypes = [
        ctypes.c_void_p,
        COORD,
    ]
    _ResizePseudoConsole.restype = ctypes.c_long

    _ClosePseudoConsole = kernel32.ClosePseudoConsole
    _ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    _ClosePseudoConsole.restype = None

    _UpdateProcThreadAttribute = kernel32.UpdateProcThreadAttribute
    _UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,  # AttributeList
        ctypes.wintypes.DWORD,  # AttributeFlags
        ctypes.c_void_p,  # Attribute
        ctypes.POINTER(ctypes.c_void_p),  # Value (pointer to HPCON)
        ctypes.c_size_t,  # Size
        ctypes.c_void_p,  # PreviousValue
        ctypes.c_void_p,  # ReturnSize
    ]
    _UpdateProcThreadAttribute.restype = ctypes.wintypes.BOOL

    _InitializeProcThreadAttributeList = kernel32.InitializeProcThreadAttributeList
    _InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _InitializeProcThreadAttributeList.restype = ctypes.wintypes.BOOL

    _DeleteProcThreadAttributeList = kernel32.DeleteProcThreadAttributeList
    _DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _DeleteProcThreadAttributeList.restype = None

    _CreatePipe = kernel32.CreatePipe
    _CreatePipe.argtypes = [
        ctypes.POINTER(ctypes.wintypes.HANDLE),
        ctypes.POINTER(ctypes.wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.wintypes.DWORD,
    ]
    _CreatePipe.restype = ctypes.wintypes.BOOL

    _SetHandleInformation = kernel32.SetHandleInformation
    _SetHandleInformation.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    _SetHandleInformation.restype = ctypes.wintypes.BOOL

    _CreateProcessW = kernel32.CreateProcessW
    _CreateProcessW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPWSTR,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    _CreateProcessW.restype = ctypes.wintypes.BOOL

    _WaitForSingleObject = kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
    _WaitForSingleObject.restype = ctypes.wintypes.DWORD

    _GetExitCodeProcess = kernel32.GetExitCodeProcess
    _GetExitCodeProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
    _GetExitCodeProcess.restype = ctypes.wintypes.BOOL

    _TerminateProcess = kernel32.TerminateProcess
    _TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    _TerminateProcess.restype = ctypes.wintypes.BOOL

    _CloseHandle = kernel32.CloseHandle
    _CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    _CloseHandle.restype = ctypes.wintypes.BOOL

    def _win32_spawn(
        *,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None,
        columns: int,
        rows: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float,
    ) -> PtySession:
        """Start ``argv`` inside a pseudo-terminal at ``cwd`` (Windows ConPTY).

        Falls back to pipe-based subprocess execution if ConPTY is not available
        or fails on this Windows version.
        """

        # Create inheritable pipe pairs for fallback and for ConPTY.
        #  - input pipe: parent writes to ``in_write``, console reads from ``in_read``
        #  - output pipe: console writes to ``out_write``, parent reads from ``out_read``
        sec = SECURITY_ATTRIBUTES()
        sec.nLength = ctypes.sizeof(sec)
        sec.bInheritHandle = True
        sec.lpSecurityDescriptor = None

        in_read = ctypes.wintypes.HANDLE()
        in_write = ctypes.wintypes.HANDLE()
        out_read = ctypes.wintypes.HANDLE()
        out_write = ctypes.wintypes.HANDLE()
        if not _CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), ctypes.byref(sec), 0):
            raise PtyUnavailable("CreatePipe (input) failed")
        if not _CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), ctypes.byref(sec), 0):
            raise PtyUnavailable("CreatePipe (output) failed")

        # Make the read end of the input pipe non-inheritable (parent reads it
        # to feed the PTY input); the write end stays inheritable so the
        # console's input side can be attached. Symmetrically for output.
        _SetHandleInformation(in_read, HANDLE_FLAG_INHERIT, 0)
        _SetHandleInformation(out_write, HANDLE_FLAG_INHERIT, 0)

        try:
            return _win32_spawn_conpty(
                argv=argv,
                cwd=cwd,
                env=env,
                columns=columns,
                rows=rows,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
                in_read=in_read,
                in_write=in_write,
                out_read=out_read,
                out_write=out_write,
            )
        except (OSError, PtyUnavailable):
            # ConPTY failed (e.g., unsupported Windows). hpc/pipes owned by
            # _win32_spawn_conpty are closed there; close our pipe ends here
            # exactly once, then fall back to pipe-based execution.
            for h in (in_read, in_write, out_read, out_write):
                with contextlib.suppress(OSError):
                    _CloseHandle(h)
            # Fall back to pipe-based subprocess execution.
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            env_final = env or os.environ.copy()
            for name in _CREDENTIAL_ENV_NAMES:
                env_final.pop(name, None)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env_final,
                creationflags=creation_flags,
            )
            return _pipe_session(
                process=process,
                argv=argv,
                cwd=str(cwd),
                columns=columns,
                rows=rows,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )

    def _win32_spawn_conpty(
        *,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None,
        columns: int,
        rows: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float,
        in_read: ctypes.wintypes.HANDLE,
        in_write: ctypes.wintypes.HANDLE,
        out_read: ctypes.wintypes.HANDLE,
        out_write: ctypes.wintypes.HANDLE,
    ) -> PtySession:
        """Create a pseudo-terminal session using the Windows ConPTY API."""

        # Create the pseudo console. Console reads input from in_read; writes
        # output to out_write.
        size = COORD(X=columns, Y=rows)
        hpc = ctypes.c_void_p()
        hr = _CreatePseudoConsole(size, in_read, out_write, 0, ctypes.byref(hpc))
        if hr != 0:
            raise PtyUnavailable(f"CreatePseudoConsole failed (hr={hr})")

        _attr_initialized = False
        attr_list: ctypes.c_void_p | None = None
        try:
            # Size the attribute list per the documented two-call pattern:
            # query with NULL, allocate exactly, then initialize in place.
            # The buffer must outlive CreateProcessW and is deleted only
            # when Initialize succeeded (deleting an uninitialized list
            # would double-free).
            size_needed = ctypes.c_size_t(0)
            _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size_needed))
            buf_size = max(int(size_needed.value), 64)
            buf = ctypes.create_string_buffer(buf_size)
            attr_list = ctypes.cast(buf, ctypes.c_void_p)
            size_needed = ctypes.c_size_t(buf_size)
            if not _InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(size_needed)):
                last_err = ctypes.get_last_error()
                raise PtyUnavailable(f"InitializeProcThreadAttributeList failed (err={last_err})")
            _attr_initialized = True
            try:
                if not _UpdateProcThreadAttribute(
                    attr_list,
                    0,
                    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    ctypes.byref(hpc),
                    ctypes.sizeof(ctypes.c_void_p),
                    None,
                    None,
                ):
                    raise PtyUnavailable("UpdateProcThreadAttribute failed")

                # Build the command line.
                cmdline = " ".join(f'"{arg}"' if " " in arg else arg for arg in argv)

                si = STARTUPINFOEXW()
                si.StartupInfo.cb = ctypes.sizeof(si)
                si.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
                si.StartupInfo.hStdInput = in_read
                si.StartupInfo.hStdOutput = out_write
                si.StartupInfo.hStdError = out_write
                si.lpAttributeList = attr_list

                pi = PROCESS_INFORMATION()
                env_block = None
                if env is not None:
                    parts = [f"{k}={v}" for k, v in env.items()]
                    env_block = ("\x00".join(parts) + "\x00\x00").encode("utf-16-le")

                created = _CreateProcessW(
                    None,
                    cmdline,
                    None,
                    None,
                    True,
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                    env_block,
                    cwd,
                    ctypes.byref(si),
                    ctypes.byref(pi),
                )
                if not created:
                    raise PtyUnavailable(f"CreateProcessW failed (err={ctypes.get_last_error()})")

                try:
                    _CloseHandle(pi.hThread)
                    # Convert the console output read end and input write end
                    # to Python fds for the session to read/write.
                    read_fd = msvcrt.open_osfhandle(int(out_read), os.O_RDONLY)
                    write_fd = msvcrt.open_osfhandle(int(in_write), os.O_WRONLY)
                except Exception:
                    _TerminateProcess(pi.hProcess, 1)
                    _CloseHandle(pi.hProcess)
                    raise

                session = PtySession(
                    read_fd=read_fd,
                    write_fd=write_fd,
                    process_handle=pi.hProcess,
                    pid=pi.dwProcessId,
                    columns=columns,
                    rows=rows,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                    conpty_handle=hpc.value,
                )
                session.start_windows_reader()
                return session
            finally:
                if _attr_initialized and attr_list is not None:
                    _DeleteProcThreadAttributeList(attr_list)
                    _attr_initialized = False
                    attr_list = None
                # Keep buf referenced until after CreateProcessW above;
                # it is freed on scope exit once the list is deleted.
        except Exception:
            _ClosePseudoConsole(hpc)
            raise
        finally:
            # Close the pipe ends we no longer need in the parent. These are
            # HANDLEs, not fds, so close them with _CloseHandle (os.close
            # would corrupt the fd table and cause Bad file descriptor).
            with contextlib.suppress(Exception):
                _CloseHandle(in_read)
            with contextlib.suppress(Exception):
                _CloseHandle(out_write)

    def _pipe_session(
        *,
        process: subprocess.Popen[Any],
        argv: list[str],
        cwd: str,
        columns: int,
        rows: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float,
    ) -> PtySession:
        """Wrap a pipe-based subprocess.Popen in a PtySession-like interface.

        Used when ConPTY is not available (older Windows). Reads come from
        process.stdout, writes go to process.stdin.
        """

        # Make stdout/stdin binary for consistent byte handling.
        import msvcrt as _msvcrt

        stdout = process.stdout
        stdin = process.stdin
        with contextlib.suppress(OSError):
            if stdout is not None:
                _msvcrt.setmode(stdout.fileno(), os.O_BINARY)
        with contextlib.suppress(OSError):
            if stdin is not None:
                _msvcrt.setmode(stdin.fileno(), os.O_BINARY)

        # Create a pair of fds that mimic the PTY read/write split. The read
        # side is pumped via the child's binary stdout (never via a second
        # os.read owner of the same fd, which caused Bad file descriptor).
        write_fd = stdin.fileno() if stdin is not None else -1

        session = PtySession(
            read_fd=-1,
            write_fd=write_fd,
            process_handle=process,
            pid=process.pid,
            columns=columns,
            rows=rows,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )
        import functools

        session.start_windows_reader(pump=functools.partial(session._pipe_pump, process))
        return session

    def _win32_resize_pty(session: PtySession, columns: int, rows: int) -> None:
        hpc = getattr(session, "_hpc", None)
        if hpc is None:
            return
        size = COORD(X=columns, Y=rows)
        _ResizePseudoConsole(ctypes.c_void_p(hpc), size)

    def _close_conpty(hpc: Any) -> None:
        with contextlib.suppress(Exception):
            _ClosePseudoConsole(ctypes.c_void_p(hpc))

    def _terminate_process_group(process_handle: Any, pid: int = 0) -> None:
        # Windows: terminate via the real process handle, never via pid-as-handle.
        with contextlib.suppress(Exception):
            if process_handle is None:
                return
            if hasattr(process_handle, "terminate"):
                process_handle.terminate()
            else:
                _TerminateProcess(process_handle, 1)

    def _get_exit_code(process_handle: Any, pid: int) -> int | None:
        if hasattr(process_handle, "poll"):
            polled: int | None = process_handle.poll()
            return polled
        exit_code = ctypes.wintypes.DWORD()
        if (
            _GetExitCodeProcess(process_handle, ctypes.byref(exit_code))
            and exit_code.value != 0x00000102
        ):  # STILL_ACTIVE
            return int(exit_code.value)
        return None

    def _wait_process(process_handle: Any, timeout: float) -> None:
        if hasattr(process_handle, "wait"):
            with contextlib.suppress(Exception):
                process_handle.wait(timeout=timeout)
            return
        _WaitForSingleObject(process_handle, int(timeout * 1000))

else:
    _win32_spawn = None  # type: ignore[assignment]
    _win32_resize_pty = None  # type: ignore[assignment]
    _close_conpty = None  # type: ignore[assignment]
    _terminate_process_group = None  # type: ignore[assignment]
    _get_exit_code = None  # type: ignore[assignment]
    _wait_process = None  # type: ignore[assignment]
