"""Synchronous Runner-compatible SSH client; callbacks execute on the caller thread."""
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import threading
import time
import uuid

import ansible_runner
import paramiko

from .protocol import json_line, receive, ProtocolError, MAX_ARCHIVE


@dataclass
class Result:
    status: str
    rc: int


class Interrupted(Exception):
    def __init__(self, status):
        self.status = status


class ChannelReader:
    def __init__(self, channel, check):
        self.channel = channel
        self.check = check
        self.buffer = bytearray()
        self.stderr = bytearray()

    def fill(self):
        self.check()
        while self.channel.recv_stderr_ready():
            data = self.channel.recv_stderr(65536)
            if len(self.stderr) < 65536:
                self.stderr.extend(data[:65536 - len(self.stderr)])
        try:
            data = self.channel.recv(65536)
        except socket.timeout:
            return True
        self.buffer.extend(data)
        return bool(data)

    def read(self, size):
        while len(self.buffer) < size and self.fill():
            pass
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def readline(self, limit):
        while b'\n' not in self.buffer and len(self.buffer) < limit:
            if not self.fill():
                break
        end = self.buffer.find(b'\n')
        size = min(end + 1 if end >= 0 else len(self.buffer), limit)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class ChannelWriter(io.RawIOBase):
    name = 'ssh-executor'

    def __init__(self, channel, stopped):
        self.channel, self.stopped = channel, stopped

    def writable(self):
        return True

    def write(self, data):
        total = len(data)
        while data:
            if self.stopped.is_set():
                raise InterruptedError('Executor transmission canceled')
            try:
                size = self.channel.send(data[:65536])
            except socket.timeout:
                continue
            if not size:
                raise EOFError('Executor closed the input stream')
            data = data[size:]
        return total

    def flush(self):
        pass


def run(*, host, private_key, known_hosts, port=2222, job_id=None,
        connect_timeout=10, job_timeout=1800, uid_min=1, uid_max=65535,
        event_handler=None, status_handler=None, cancel_callback=None,
        **kwargs):
    """Transmit native Runner kwargs, securely receive events and return status/rc.

    There is no client-side UID lease expiry. sshd/PAM owns the authoritative lease,
    including after a client crash. Each job opens and closes its own SSH transport.
    """
    if not host or not private_key or not known_hosts:
        raise ValueError('Executor host, private key and known_hosts are required')
    if not 1 <= uid_min <= uid_max <= 65535:
        raise ValueError('Invalid executor UID pool')
    job_id = str(uuid.UUID(str(job_id))) if job_id else str(uuid.uuid4())
    root = Path(kwargs['private_data_dir']).resolve()
    if root == Path('/') or not root.is_dir():
        raise ValueError('A private task directory is required')
    total = 0
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('Task inputs must be regular files and directories')
        total += info.st_size if stat.S_ISREG(info.st_mode) else 0
        if total > MAX_ARCHIVE:
            raise ValueError('Task inputs exceed the 128 MiB transport limit')
    deadline = time.monotonic() + job_timeout if job_timeout and job_timeout > 0 else None
    stopped = threading.Event()
    client = None
    sender = None
    sender_errors = []
    channel = None
    last_error = None

    def check():
        if cancel_callback and cancel_callback():
            raise Interrupted('canceled')
        if deadline and time.monotonic() >= deadline:
            raise Interrupted('timeout')

    def status(data):
        if status_handler:
            status_handler(data)

    status({'status': 'starting', 'private_data_dir': str(root)})
    try:
        # A busy identity can authenticate but cannot open a PAM session. No job
        # bytes are sent until the managed worker has acknowledged the lease.
        for attempt in range(16):
            check()
            identity = uid_min + secrets.randbelow(uid_max - uid_min + 1)
            client = paramiko.SSHClient()
            client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            host_id = f'[{host}]:{port}' if int(port) != 22 else host
            if client.get_host_keys().lookup(host_id) is None:
                raise ValueError('Executor is missing from the configured known_hosts')
            try:
                client.connect(host, port=int(port), username=f'jms_{identity}',
                               key_filename=str(private_key), allow_agent=False, look_for_keys=False,
                               timeout=connect_timeout, auth_timeout=connect_timeout,
                               banner_timeout=connect_timeout)
                transport = client.get_transport()
                transport.set_keepalive(15)
                channel = transport.open_session(timeout=connect_timeout)
                channel.settimeout(0.5)
                channel.exec_command(f'jms-executor-run {job_id}')
                handshake_deadline = time.monotonic() + connect_timeout

                def check_handshake():
                    check()
                    if time.monotonic() >= handshake_deadline:
                        raise ProtocolError('Executor lease acknowledgement timed out')

                reader = ChannelReader(channel, check_handshake)
                hello = json_line(reader)
                if hello != {'executor_protocol': 1, 'job_id': job_id, 'uid': 100000 + identity}:
                    raise ProtocolError('Executor lease acknowledgement mismatch')
                reader.check = check
                break
            except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
                raise
            except (EOFError, OSError, paramiko.SSHException, ProtocolError) as exc:
                last_error = exc
                client.close()
                client = None
                if attempt == 15:
                    raise RuntimeError('Unable to acquire an executor identity') from last_error
        writer = ChannelWriter(channel, stopped)
        writer.write((json.dumps({'source_dir': os.path.abspath(kwargs['private_data_dir'])}) + '\n').encode())
        options = dict(kwargs)
        options['process_isolation'] = False
        options.pop('clean_workspace', None)
        options.pop('finished_callback', None)
        for key in list(options):
            if key.startswith('container_') or key.startswith('process_isolation_'):
                options.pop(key)
        options.pop('directory_isolation_base_path', None)
        options.pop('artifact_dir', None)

        def transmit():
            try:
                ansible_runner.run(streamer='transmit', _output=writer, **options)
                channel.shutdown_write()
            except Exception as exc:
                sender_errors.append(exc)
                channel.close()

        sender = threading.Thread(target=transmit, name='ansible-ssh-transmit', daemon=True)
        sender.start()
        final_status, rc = receive(reader, str(root), event_handler, status, kwargs.get('quiet', False))
        while not channel.exit_status_ready():
            check()
            while channel.recv_stderr_ready():
                channel.recv_stderr(65536)
            time.sleep(0.05)
        exit_code = channel.recv_exit_status()
        if sender_errors:
            raise RuntimeError('Failed to transmit executor task') from sender_errors[0]
        if exit_code != rc:
            raise ProtocolError('SSH exit status and task rc disagree')
        return Result(final_status, rc)
    except Interrupted as exc:
        status({'status': exc.status, 'private_data_dir': str(root)})
        return Result(exc.status, 254)
    except Exception:
        status({'status': 'failed', 'private_data_dir': str(root)})
        raise
    finally:
        stopped.set()
        if client:
            # PAM closes the lease, kills every remaining process of the UID and
            # removes its home. A crashed monitor is recovered by the next lease.
            client.close()
        if sender:
            sender.join(timeout=connect_timeout + 1)
