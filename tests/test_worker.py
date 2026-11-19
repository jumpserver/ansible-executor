import base64
import io
import shlex
import tempfile
import unittest
from unittest.mock import patch

from test_protocol import message, zipped

from jms_ansible_executor.worker import RelocatingWorker


class WorkerPathTests(unittest.TestCase):
    def test_duplicate_task_archive_is_rejected_before_execution(self):
        raw = zipped([('file', 'input')])
        archive = message({'zipfile': len(raw)}) + base64.b64encode(raw)
        stream = message({'kwargs': {}}) + archive + archive + message({'eof': True})
        with tempfile.TemporaryDirectory() as root, patch('ansible_runner.run') as execute:
            worker = RelocatingWorker(private_data_dir=root, _input=io.BytesIO(stream), _output=io.BytesIO())
            with self.assertRaisesRegex(ValueError, 'Duplicate task archive'):
                worker.run()
            execute.assert_not_called()

    def test_gateway_password_file_and_key_are_relocated(self):
        source = "/core/jobs/space and 'quote' 50%"
        worker = RelocatingWorker(source_dir=source, private_data_dir='/task')
        command = ['sshpass', '-f', (source + '/.gateway-password').replace('%', '%%'),
                   'ssh', '-i', (source + '/.gateway-key').replace('%', '%%'),
                   '-W', '%h:%p', '--', 'user@gateway']
        args = shlex.join(['-o', 'ProxyCommand=' + shlex.join(command)])
        actual = worker.inventory_paths({'ansible_ssh_common_args': args})
        proxy = shlex.split(actual['ansible_ssh_common_args'])[1].removeprefix('ProxyCommand=')
        expected = command.copy()
        expected[2] = '/task/.gateway-password'
        expected[5] = '/task/.gateway-key'
        self.assertEqual(shlex.split(proxy), expected)

    def test_gateway_key_paths_are_relocated_without_changing_other_arguments(self):
        source = "/core/jobs/space and 'quote' 50%"
        worker = RelocatingWorker(source_dir=source, private_data_dir='/task')
        key = source + '/.gateway'
        password = source + '/literal-password'
        command = ['sshpass', '-p', password.replace('%', '%%'), 'ssh', '-i',
                   key.replace('%', '%%'), '-W', '%h:%p', '--', 'user@gateway']
        args = shlex.join(['-o', 'ProxyCommand=' + shlex.join(command)])
        inventory = {
            'ansible_ssh_common_args': args,
            'jms_asset': {'ansible_ssh_common_args': args},
            'literal': args,
        }
        relocated = worker.inventory_paths(inventory)
        for actual in (relocated['ansible_ssh_common_args'],
                       relocated['jms_asset']['ansible_ssh_common_args']):
            proxy = shlex.split(actual)[1].removeprefix('ProxyCommand=')
            expected = command.copy()
            expected[5] = '/task/.gateway'
            self.assertEqual(shlex.split(proxy), expected)
        self.assertEqual(relocated['literal'], args)
        self.assertEqual(inventory['ansible_ssh_common_args'], args)

    def test_non_task_ssh_arguments_are_unchanged(self):
        worker = RelocatingWorker(source_dir='/core/task', private_data_dir='/task')
        for args in (
            '-o StrictHostKeyChecking=no',
            '-o "ProxyCommand=ssh -i /service/key -W %h:%p gateway"',
        ):
            self.assertEqual(worker.inventory_paths({'ansible_ssh_common_args': args}),
                             {'ansible_ssh_common_args': args})


if __name__ == '__main__':
    unittest.main()
