"""One exclusive lock per project folder, across processes.

Two ComfyUI processes can open one project (a second install, an external link). The
in-process write lock cannot keep one from deleting or overwriting what the other is
saving, so a commit and a component sweep also hold this: an OS lock on
``project.lock`` in the project folder. The OS drops it when its holder dies. Loads
never take it; component retention protects them (``project_storage``). It is taken
only while **Share Projects Between ComfyUI Servers** is on (``server_settings``; off by
default), and Sonder 0.9.3 and older never take it, so it protects only processes that
do.

``probe_locks`` checks a folder's drive before that setting is turned on.

The file is never written and never deleted: deleting it would let two processes hold
locks on two different files.
"""
import errno
import os
import stat
import time
import uuid

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

LOCK_FILENAME = "project.lock"

HELD = "held"
CONTENDED = "contended"
UNSUPPORTED = "unsupported"

# The filesystem itself says it has no locks. Every other failure is ambiguous and
# counts as contended: ENOLCK or EINVAL can also be a passing condition, and reading
# one as "no locks here" would let a save run beside a peer that holds the lock.
_UNSUPPORTED = {getattr(errno, name) for name in ("ENOTSUP", "EOPNOTSUPP", "ENOSYS") if hasattr(errno, name)}
# ERROR_INVALID_FUNCTION and ERROR_NOT_SUPPORTED. ``msvcrt.locking`` cannot tell them
# from anything else (it reports EINVAL), which is why Windows calls LockFileEx.
_UNSUPPORTED_WINDOWS = {1, 50}

# An indexer, antivirus or backup can hold the file for a moment.
_OPEN_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2)

# Busy is only possible while Share Projects Between ComfyUI Servers is on.
PROJECT_BUSY_MESSAGE = (
    "Another ComfyUI server is saving this project. Try again in a moment. If no other server "
    "uses it, turn off Share Projects Between ComfyUI Servers in the editor's Settings ▸ Server.")


class ProjectBusyError(PermissionError):
    """Another process held the project lock for the whole wait. Nothing was published.

    A ``PermissionError`` because that is what a contended save already raises when
    ``atomic_replace`` runs out of attempts, so callers keep one policy for both.
    """

    def __init__(self, message=PROJECT_BUSY_MESSAGE):
        super().__init__(message)


class ProjectFileLock:
    """Not reentrant, not thread-safe: one instance per attempt, under the thread lock."""

    def __init__(self, project_dir):
        self._path = os.path.join(project_dir, LOCK_FILENAME)
        self._fd = None

    def try_acquire(self):
        """``HELD``, ``CONTENDED`` or ``UNSUPPORTED``. An open failure raises as itself:
        on Windows it has the same errno as contention, so the two are never mixed."""
        fd = self._open()
        try:
            outcome = _lock(fd)
        except BaseException:
            os.close(fd)
            raise
        if outcome == HELD:
            self._fd = fd
        else:
            os.close(fd)
        return outcome

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)  # explicit: Windows may delay a lock it releases because of a close
        finally:
            os.close(fd)  # drops it in any case

    def _open(self):
        for delay in (*_OPEN_RETRY_DELAYS, None):
            self._refuse_link()
            try:
                return self._open_once()
            except PermissionError as exc:
                if delay is None:
                    # Without the path: handlers show an OSError's text.
                    raise PermissionError(exc.errno, f"{LOCK_FILENAME} could not be opened") from exc
                time.sleep(delay)

    def _open_once(self):
        flags = (getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            return os.open(self._path, flags | os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as exc:
            # Another user's lock file in a shared folder: locking needs no write access.
            if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS) or not os.path.isfile(self._path):
                raise
            return os.open(self._path, flags | os.O_RDONLY)

    def _refuse_link(self):
        try:
            info = os.lstat(self._path)
        except FileNotFoundError:
            return
        # A symlink is not a regular file to lstat. Other reparse points are not links: a
        # cloud-synced folder can mark an ordinary file as one.
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.ELOOP, "project.lock is not a regular file")


if os.name == "nt":
    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
    _EXCLUSIVE_NO_WAIT = 0x2 | 0x1

    def _range():
        # Far past the end: a locked byte cannot be read, and the empty file must stay
        # copyable with the project while it is held.
        return ctypes.byref(_Overlapped(OffsetHigh=1))

    def _lock(fd):
        error = _lock_file(fd)
        if not error:
            return HELD
        return UNSUPPORTED if error in _UNSUPPORTED_WINDOWS else CONTENDED

    def _lock_file(fd):
        """0, or the Windows error code."""
        if _kernel32.LockFileEx(msvcrt.get_osfhandle(fd), _EXCLUSIVE_NO_WAIT, 0, 1, 0, _range()):
            return 0
        return ctypes.get_last_error() or -1

    def _unlock(fd):
        _kernel32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, _range())

    def _raw_lock(fd):
        """``None``, or the Windows error code (for `probe_locks`)."""
        return _lock_file(fd) or None

    def _is_conflict(error):
        return error == 33  # ERROR_LOCK_VIOLATION

    def _says_no_locks(error):
        return error in _UNSUPPORTED_WINDOWS
else:
    def _lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return UNSUPPORTED if exc.errno in _UNSUPPORTED else CONTENDED
        return HELD

    def _unlock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def _raw_lock(fd):
        """``None``, or the errno (for `probe_locks`)."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno
        return None

    def _is_conflict(error):
        return error in (errno.EWOULDBLOCK, errno.EAGAIN)

    def _says_no_locks(error):
        return error in _UNSUPPORTED


PROBE_PREFIX = ".sonder-lock-probe-"
PROBE_OK = "ok"
PROBE_UNSUPPORTED = "unsupported"
PROBE_FAILED = "check_failed"


def probe_locks(directory):
    """Whether file locks work in ``directory`` on this machine: ``(outcome, detail)``.

    Two independent handles on a new file. The first must take the lock, the second must
    get the platform's own conflict answer (not any error: production counts unclear
    errors as busy, which here would pass a drive whose locks do not work), and the second
    must take it once the first lets go. ``PROBE_UNSUPPORTED`` when the drive says it has
    no locks or does not enforce them; ``PROBE_FAILED`` for anything else, with ``detail``
    for the server log only (it can name a path). A pass says nothing about other hosts:
    some network mounts enforce locks locally only.

    Only the file this call created is removed; nothing else is touched.
    """
    path = os.path.join(directory, f"{PROBE_PREFIX}{uuid.uuid4().hex}")
    flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    first = second = None
    created = False
    try:
        try:
            first = os.open(path, flags | os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
            created = True
            second = os.open(path, flags | os.O_RDWR)
        except OSError as exc:
            return PROBE_FAILED, f"could not create a test file: {exc}"
        error = _raw_lock(first)
        if error is not None:
            outcome = PROBE_UNSUPPORTED if _says_no_locks(error) else PROBE_FAILED
            return outcome, f"lock failed with {error}"
        error = _raw_lock(second)
        if error is None:
            return PROBE_UNSUPPORTED, "a second handle took the lock while the first held it"
        if not _is_conflict(error):
            return PROBE_FAILED, f"a second handle failed with {error} instead of a lock conflict"
        _unlock(first)
        error = _raw_lock(second)
        if error is not None:
            return PROBE_FAILED, f"the lock could not be taken again after release ({error})"
        _unlock(second)
        return PROBE_OK, ""
    finally:
        for fd in (second, first):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if created:
            try:
                os.remove(path)
            except OSError:
                pass
