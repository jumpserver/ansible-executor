"""Add an ENOSYS filter on top of Docker's seccomp policy to emulate EL8."""
import ctypes
import errno
import os
import platform
import sys


class Filter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]


class Program(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]


if platform.machine() not in ('x86_64', 'aarch64'):
    raise RuntimeError('The EL8 test filter supports the executor CI architectures only')
# pidfd_open has the same syscall number on x86_64 and aarch64. Other calls
# remain subject to Docker's existing filter; this adds no permissions.
filters = (Filter * 4)(
    Filter(0x20, 0, 0, 0),                # load seccomp_data.nr
    Filter(0x15, 0, 1, 434),              # pidfd_open
    Filter(0x06, 0, 0, 0x50000 | errno.ENOSYS),
    Filter(0x06, 0, 0, 0x7fff0000),       # allow (subject to prior filters)
)
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(38, 1, 0, 0, 0) != 0:     # PR_SET_NO_NEW_PRIVS
    raise OSError(ctypes.get_errno(), 'Unable to set no-new-privileges')
if libc.prctl(22, 2, ctypes.byref(Program(len(filters), filters)), 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'Unable to install the EL8 test filter')
try:
    fd = os.pidfd_open(os.getpid())
except OSError as exc:
    if exc.errno != errno.ENOSYS:
        raise
else:
    os.close(fd)
    raise RuntimeError('The EL8 test filter did not deny pidfd_open')
command = sys.argv[1:] or ['/usr/local/libexec/jms-executor-start']
os.execv(command[0], command)
