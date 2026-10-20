import shlex
import unittest

from jms_ansible_executor.worker import RelocatingWorker


class WorkerPathTests(unittest.TestCase):
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
