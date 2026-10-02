"""Observe an owned child before reaping it. Stdlib-only worker support.

Darwin's libc exposes waitid even where CPython omits os.waitid. Its public
64-bit siginfo_t layout is in sys/signal.h; wait flags are in sys/wait.h.
No numeric PID is signalled by this module. Unsupported platforms fail closed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys


class _SigValue(ctypes.Union):
    _fields_ = [("integer", ctypes.c_int), ("pointer", ctypes.c_void_p)]


class _DarwinSigInfo(ctypes.Structure):
    _fields_ = [
        ("signo", ctypes.c_int),
        ("error", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("address", ctypes.c_void_p),
        ("value", _SigValue),
        ("band", ctypes.c_long),
        ("pad", ctypes.c_ulong * 7),
    ]


def exited_without_reaping(pid: int) -> bool:
    """Caller must hold Popen's wait lock and still own this direct child.

    True means waitid observed that exact waitable child, with WNOWAIT. False
    means no exit yet. ECHILD/unsupported never provides an identity proof.
    """
    if type(pid) is not int or pid <= 1:
        raise ChildProcessError("invalid child identity")
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        result = waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return False
        if (
            result.si_pid != pid
            or result.si_signo != signal.SIGCHLD
            or result.si_code not in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED)
        ):
            raise ChildProcessError("unexpected child identity")
        return True
    if (
        sys.platform != "darwin"
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(_DarwinSigInfo) != 104
        or _DarwinSigInfo.pid.offset != 12
        or (os.P_PID, os.WEXITED, os.WNOHANG, os.WNOWAIT) != (1, 4, 1, 32)
    ):
        raise NotImplementedError("non-reaping child observation unsupported")
    function = ctypes.CDLL(None, use_errno=True).waitid
    function.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(_DarwinSigInfo),
        ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    info = _DarwinSigInfo()
    rc = function(
        os.P_PID, pid, ctypes.byref(info), os.WEXITED | os.WNOHANG | os.WNOWAIT
    )
    if rc != 0:
        code = ctypes.get_errno()
        if code == errno.ECHILD:
            raise ChildProcessError("child identity unavailable")
        raise OSError(code, "non-reaping child observation failed")
    if info.pid == 0:
        return False
    if info.pid != pid or info.signo != 20 or info.code not in (1, 2, 3):
        raise ChildProcessError("unexpected child observation")
    return True
