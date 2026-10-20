import base64
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
import zipfile

from jms_ansible_executor.protocol import ProtocolError, receive, safe_archive


def zipped(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return data.getvalue()


def message(value):
    return (json.dumps(value) + '\n').encode()


class ProtocolTest(unittest.TestCase):
    def test_rejects_path_traversal_absolute_duplicates_and_symlinks(self):
        symlink = zipfile.ZipInfo('link')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in ([('../escaped', 'secret')], [('/tmp/escaped', 'secret')],
                        [('safe', 'a'), ('safe', 'b')], [(symlink, '../escaped')],
                        [('a\\b', 'a')], [('.', 'a')]):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as root:
                raw = zipped(entries)
                with self.assertRaises(ProtocolError):
                    safe_archive(io.BytesIO(base64.b64encode(raw)), len(raw), root)
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_rejects_existing_symlink_parent_and_truncated_archive(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root) / 'dir').symlink_to(outside, target_is_directory=True)
            raw = zipped([('dir/file', 'secret')])
            with self.assertRaises(ProtocolError):
                safe_archive(io.BytesIO(base64.b64encode(raw)), len(raw), root)
            self.assertFalse((Path(outside) / 'file').exists())
            with self.assertRaises(ProtocolError):
                safe_archive(io.BytesIO(b'AA'), len(raw), root)

    def test_rejects_unsafe_event_names_counters_and_uuids(self):
        valid = {'event': 'runner_on_ok', 'counter': 1, 'uuid': str(uuid.uuid4())}
        for patch in ({'counter': '../../../escaped'}, {'counter': True},
                      {'uuid': '../escaped'}, {'event': '../escaped'},
                      {'event': 'write_pid'}, {'event_data': []}):
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(ProtocolError):
                    receive(io.BytesIO(message(valid | patch)), root, quiet=True)

    def test_streams_callbacks_without_remote_pid_or_environment_and_checks_rc(self):
        event = {'event': 'runner_on_ok', 'counter': 1, 'uuid': str(uuid.uuid4()), 'pid': 123}
        raw = zipped([('rc', '2'), ('status', 'failed')])
        stream = (message({'status': 'failed', 'env': {'SECRET': 'hidden'}, 'pid': 321})
                  + message(event) + message({'zipfile': len(raw)})
                  + base64.b64encode(raw) + message({'eof': True}))
        events, statuses = [], []
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(receive(io.BytesIO(stream), root, events.append, statuses.append, True),
                             ('failed', 2))
            self.assertNotIn('pid', events[0])
            self.assertNotIn('env', statuses[0])
            self.assertNotIn('pid', statuses[0])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ProtocolError):
                receive(io.BytesIO(stream.replace(b'"failed"', b'"successful"')), root, quiet=True)

    def test_missing_terminal_status_or_rc_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ProtocolError):
                receive(io.BytesIO(message({'eof': True})), root)


if __name__ == '__main__':
    unittest.main()
