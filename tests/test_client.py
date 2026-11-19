from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jms_ansible_executor.client import run
from jms_ansible_executor.protocol import MAX_TASK_INPUT


class ClientInputTests(unittest.TestCase):
    def test_oversized_and_symlink_inputs_are_rejected_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory, patch('paramiko.SSHClient') as client:
            root = Path(directory)
            image = root / 'image'
            with image.open('wb') as stream:
                stream.truncate(MAX_TASK_INPUT + 1)
            options = dict(host='executor', private_key='key', known_hosts='hosts',
                           private_data_dir=directory)
            with self.assertRaisesRegex(ValueError, '32 GiB'):
                run(**options)
            image.unlink()
            image.symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'regular files'):
                run(**options)
            client.assert_not_called()
