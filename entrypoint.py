#!/usr/bin/python3 -I
"""Bootstrap the dedicated executor container, not a host SSH installation."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import stat
import subprocess


HOME_ROOT = Path('/home/jms-executor')
KEY_ROOT = Path('/var/lib/jms-executor')
RUNTIME_ROOT = Path('/run/jms-executor')
UID_BASE = 100000
ID_LIMIT = 65535


def root_directory(path, mode):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise RuntimeError(f'Expected a root-owned directory: {path}')
    path.chmod(mode)


def configure_nss():
    """Used at image build time only. Keep files first for system identities."""
    path = Path('/etc/nsswitch.conf')
    lines = path.read_text().splitlines()
    for database in ('passwd', 'group', 'initgroups'):
        lines = [line for line in lines if line.split(':', 1)[0].strip() != database]
        lines.append(f'{database}: files jms_executor')
    path.write_text('\n'.join(lines) + '\n')


def check_reserved_identities():
    # Virtual identities are deliberately not enumerated by NSS.
    for account in pwd.getpwall():
        if (UID_BASE < account.pw_uid <= UID_BASE + ID_LIMIT
                or UID_BASE < account.pw_gid <= UID_BASE + ID_LIMIT
                or account.pw_name.startswith('jms_')):
            raise RuntimeError('A system account conflicts with the executor UID range')
    for group in grp.getgrall():
        if (UID_BASE < group.gr_gid <= UID_BASE + ID_LIMIT
                or group.gr_name.startswith('jms_')):
            raise RuntimeError('A system group conflicts with the executor GID range')
    account = pwd.getpwnam('jms_1')
    if account.pw_uid != UID_BASE + 1 or account.pw_dir != str(HOME_ROOT / 'jms_1'):
        raise RuntimeError('The executor NSS plugin is not configured')


def install_public_keys(source):
    keys = []
    for line in source.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split()
        if len(fields) < 2 or fields[0] not in (
            'ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256',
            'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
        ):
            raise RuntimeError('Expected bare SSH public keys, without authorized_keys options')
        key = f'{fields[0]} {fields[1]}'
        subprocess.run(
            ['/usr/bin/ssh-keygen', '-l', '-f', '/dev/stdin'],
            input=key + '\n', text=True, check=True, stdout=subprocess.DEVNULL,
        )
        keys.append(f'restrict {key}\n')
    if not keys:
        raise RuntimeError('At least one service public key is required')
    destination = RUNTIME_ROOT / 'authorized_keys'
    # The parent is writable only by root; jobs cannot replace this file.
    destination.write_text(''.join(keys))
    destination.chmod(0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure-nss', action='store_true')
    parser.add_argument('--public-keys', type=Path, default=Path('/run/secrets/jms-executor.pub'))
    args = parser.parse_args()
    if os.getuid() != 0:
        parser.error('sshd startup needs container root; privileged mode is not required')
    if args.configure_nss:
        configure_nss()
        return
    os.umask(0o077)
    root_directory(HOME_ROOT, 0o711)
    root_directory(KEY_ROOT, 0o700)
    root_directory(RUNTIME_ROOT, 0o755)
    root_directory(RUNTIME_ROOT / 'leases', 0o700)
    root_directory(Path('/run/sshd'), 0o755)
    root_directory(Path('/etc/jms-executor/skel'), 0o755)
    check_reserved_identities()
    install_public_keys(args.public_keys)
    host_key = KEY_ROOT / 'ssh_host_ed25519_key'
    if not host_key.exists():
        subprocess.run(
            ['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)],
            check=True,
        )
    info = host_key.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
        raise RuntimeError('The SSH host key must be a root-owned regular file')
    host_key.chmod(0o600)
    config = '/etc/ssh/jms-executor.conf'
    subprocess.run(['/usr/sbin/sshd', '-t', '-f', config], check=True)
    os.execve('/usr/sbin/sshd', ['/usr/sbin/sshd', '-D', '-e', '-f', config], {
        'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
    })


if __name__ == '__main__':
    main()
