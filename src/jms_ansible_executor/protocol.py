"""Bounded Runner stream decoding. Remote events/archives are untrusted input."""
import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import uuid
import zipfile

MAX_LINE = 8 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


def json_line(stream):
    line = stream.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ProtocolError('Truncated or oversized executor message')
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError('Invalid executor JSON') from exc
    if not isinstance(value, dict):
        raise ProtocolError('Executor message must be an object')
    return value


def safe_archive(stream, length, destination):
    if type(length) is not int or not 0 < length <= MAX_ARCHIVE:
        raise ProtocolError('Invalid artifact archive length')
    with tempfile.TemporaryFile() as archive:
        encoded_remaining = 4 * ((length + 2) // 3)
        decoded_size = 0
        while encoded_remaining:
            size = min(65536, encoded_remaining)
            data = stream.read(size)
            if len(data) != size:
                raise ProtocolError('Truncated artifact archive')
            try:
                decoded = base64.b64decode(data, validate=True)
            except ValueError as exc:
                raise ProtocolError('Invalid artifact encoding') from exc
            archive.write(decoded)
            decoded_size += len(decoded)
            encoded_remaining -= size
        if decoded_size != length:
            raise ProtocolError('Artifact length mismatch')
        archive.seek(0)
        if Path(destination).is_symlink():
            raise ProtocolError('Artifact destination is a symlink')
        root = Path(destination).resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
            if len(members) > 100000 or sum(item.file_size for item in members) > MAX_EXPANDED:
                raise ProtocolError('Artifact archive exceeds resource limits')
            seen = set()
            # Validate the whole manifest before extracting even a single file.
            for item in members:
                name = item.orig_filename
                path = PurePosixPath(name)
                kind = stat.S_IFMT(item.external_attr >> 16)
                if (not name or str(path) == '.' or '\\' in name or '\x00' in name or ':' in name
                        or path.is_absolute() or '..' in path.parts
                        or str(path) in seen or kind not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ProtocolError('Unsafe artifact entry')
                seen.add(str(path))
            for item in members:
                path = root.joinpath(*PurePosixPath(item.filename).parts)
                # No symlinks may be followed, including ones predating the archive.
                for parent in reversed([path.parent, *path.parent.parents]):
                    if parent == root or root in parent.parents:
                        if parent.is_symlink():
                            raise ProtocolError('Artifact parent is a symlink')
                        parent.mkdir(mode=0o700, exist_ok=True)
                if not path.resolve().is_relative_to(root):
                    raise ProtocolError('Artifact escapes job directory')
                if item.is_dir():
                    if path.is_symlink():
                        raise ProtocolError('Artifact directory is a symlink')
                    path.mkdir(mode=0o700, exist_ok=True)
                    continue
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output, zipped.open(item) as source:
                    remaining = item.file_size
                    while remaining:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            raise ProtocolError('Truncated artifact member')
                        output.write(chunk)
                        remaining -= len(chunk)


def receive(stream, private_data_dir, event_handler=None, status_handler=None, quiet=False):
    artifacts = Path(private_data_dir) / 'artifacts'
    events = artifacts / 'job_events'
    if artifacts.is_symlink() or events.is_symlink():
        raise ProtocolError('Artifact directory is a symlink')
    events.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = 'starting'
    while True:
        data = json_line(stream)
        if 'status' in data:
            status = data['status']
            if status not in ('starting', 'running', 'successful', 'failed', 'canceled', 'timeout', 'error'):
                raise ProtocolError('Invalid executor status')
            if status_handler:
                # Never forward a remote environment, PID, cwd or command to Core.
                status_handler({'status': 'failed' if status == 'error' else status,
                                'job_explanation': str(data.get('job_explanation', ''))[:4096]})
        elif 'zipfile' in data:
            safe_archive(stream, data['zipfile'], artifacts)
        elif data.get('eof') is True:
            break
        elif data.get('event') == 'keepalive':
            continue
        else:
            counter = data.get('counter')
            identifier = data.get('uuid')
            event = data.get('event')
            if (type(counter) is not int or not 0 < counter < 2**63
                    or not isinstance(identifier, str)
                    or not isinstance(event, str)
                    or not re.fullmatch('[a-z][a-z0-9_]{0,127}', event)
                    or not (event.startswith(('runner_', 'playbook_'))
                            or event in ('verbose', 'debug', 'warning', 'system_warning', 'deprecated', 'error'))):
                raise ProtocolError('Invalid executor event')
            try:
                identifier = str(uuid.UUID(identifier))
            except ValueError as exc:
                raise ProtocolError('Invalid executor event UUID') from exc
            data.pop('pid', None)
            data['uuid'] = identifier
            if not isinstance(data.get('event_data', {}), dict):
                raise ProtocolError('Invalid event payload')
            if not quiet and isinstance(data.get('stdout'), str):
                print(data['stdout'])
            keep = event_handler(data) if event_handler else True
            if keep is not False:
                path = events / f'{counter}-{identifier}.json'
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(data, output)
    try:
        raw = (artifacts / 'rc').read_bytes()
        if len(raw) > 16:
            raise ValueError
        rc = int(raw)
    except (OSError, ValueError) as exc:
        raise ProtocolError('Executor did not return a valid task rc') from exc
    if not 0 <= rc <= 255 or status not in ('successful', 'failed', 'canceled', 'timeout'):
        raise ProtocolError('Executor did not finish the task')
    if status == 'successful' and rc != 0:
        raise ProtocolError('Executor status and rc disagree')
    return status, rc
