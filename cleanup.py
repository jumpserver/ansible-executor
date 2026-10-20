#!/usr/bin/python3 -I
"""Root-only PAM helper. Kill all tasks of a leased UID before deleting its home."""
import errno
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import time


def clean_shared(uid, deadline):
    # A task can bypass TMPDIR. UID reuse must not expose its 0600 files, FIFOs
    # or shared-memory objects in the image's public directories.
    for shared in ('/tmp', '/var/tmp', '/dev/shm', '/dev/mqueue', '/run/lock'):
        if not Path(shared).exists():
            continue
        def check_error(exc):
            if exc.errno != errno.ENOENT:
                raise exc
        # Directory FDs bind operations to the walked directory; no symlinks are followed.
        for _, directories, files, directory_fd in os.fwalk(
                shared, topdown=False, follow_symlinks=False, onerror=check_error):
            if time.monotonic() >= deadline:
                raise RuntimeError('Shared task cleanup timed out; refusing UID reuse')
            for name in files + directories:
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if info.st_uid != uid:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        os.rmdir(name, dir_fd=directory_fd)
                    else:
                        os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass


def clean(uid):
    if os.geteuid() != 0 or not 100001 <= uid <= 165535:
        raise RuntimeError('Invalid cleanup identity')
    deadline = time.monotonic() + 5
    while True:
        alive = False
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():
                continue
            fd = None
            try:
                # pidfd binds the signal to this process, even if its numeric PID is reused.
                fd = os.pidfd_open(int(entry.name))
                status = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines())
                if str(uid) not in status['Uid'].split() or status['State'].strip().startswith('Z'):
                    continue
                alive = True
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
            finally:
                if fd is not None:
                    os.close(fd)
        if not alive:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Executor UID still has live processes; refusing reuse')
        time.sleep(0.05)
    home = Path('/home/jms-executor') / f'jms_{uid - 100000}'
    if home.is_symlink():
        raise RuntimeError('Unexpected home symlink')
    if home.exists():
        # Python's fd-based rmtree does not follow job-controlled symlinks.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError('Safe directory cleanup is unavailable')
        shutil.rmtree(home)
    clean_shared(uid, time.monotonic() + 5)


if __name__ == '__main__':
    clean(int(sys.argv[1]))
