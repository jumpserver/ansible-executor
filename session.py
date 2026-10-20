#!/usr/bin/python3 -I
"""Restricted login shell: no shell parsing, user startup scripts, or SSH env."""
import ctypes
import json
import os
from pathlib import Path
import pwd
import stat
import sys


def prepare_session():
    uid, gid = os.getuid(), os.getgid()
    if not 100001 <= uid <= 165535 or gid != uid:
        raise RuntimeError('Invalid executor identity')
    if os.geteuid() != uid or os.getegid() != gid or set(os.getgroups()) - {gid}:
        raise RuntimeError('Unexpected executor privileges')
    account = pwd.getpwuid(uid)
    home = Path('/home/jms-executor') / f'jms_{uid - 100000}'
    if account.pw_dir != str(home):
        raise RuntimeError('Unexpected executor home')
    info = home.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError('Expected a private, task-owned home directory')

    # OpenSSH drops credentials. Verify capabilities, then prevent any exec-time
    # privilege gain through setuid binaries or file capabilities.
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    if any(int(status[name].strip(), 16) for name in ('CapEff', 'CapPrm', 'CapAmb')):
        raise RuntimeError('Task retained Linux capabilities')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), 'Unable to disable privilege escalation')

    os.umask(0o077)
    temp = home / 'tmp'
    try:
        temp.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = temp.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError('Expected a private temporary directory')
    os.chdir(home)
    return account, home, temp


def main():
    # OpenSSH invokes the NSS-provided login shell with -c and the forced command.
    if sys.argv[1:] != ['-c', 'jms-executor-session']:
        raise RuntimeError('Only the executor forced command is supported')
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if command not in ('ansible-runner worker', 'jms-executor-info'):
        raise RuntimeError('Only ansible-runner worker and jms-executor-info are supported')
    account, home, temp = prepare_session()
    environment = {
        'HOME': str(home), 'USER': account.pw_name, 'LOGNAME': account.pw_name,
        'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'TMPDIR': str(temp), 'ANSIBLE_LOCAL_TEMP': str(temp / 'ansible'),
    }
    os.environ.clear()
    os.environ.update(environment)
    if command == 'jms-executor-info':
        print(json.dumps({
            'user': account.pw_name, 'uid': os.getuid(), 'gid': os.getgid(),
            'groups': os.getgroups(), 'home': str(home),
        }))
        return
    # A new Python interpreter cannot import user files from cwd or PYTHONPATH
    # before ansible-runner has received and prepared the job.
    os.execve(sys.executable, [sys.executable, '-I', '-m', 'ansible_runner', 'worker'], environment)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Executor session rejected: {exc}', file=sys.stderr)
        sys.exit(1)
