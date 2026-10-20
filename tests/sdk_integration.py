"""Real SDK and upstream/fork Runner interoperability checks."""
import json
from pathlib import Path
import shlex
import tempfile
import threading
import time
import unittest

import paramiko
from jms_ansible_executor.client import run
from integration import ExecutorSSHTest as Fixture


class SDKTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Fixture.setUpClass()
        cls.addClassCleanup(Fixture.doClassCleanups)
        cls.options = dict(host='127.0.0.1', port=int(Fixture.ssh_options[2]),
                           private_key=Fixture.key, known_hosts=Fixture.known_hosts,
                           uid_min=10, uid_max=10, quiet=True)

    def prepare(self, root):
        root = Path(root)
        (root / 'inventory').mkdir()
        inventory = root / 'inventory/hosts'
        inventory.write_text(json.dumps({'all': {'hosts': {'localhost': {
            'ansible_connection': 'local', 'ansible_python_interpreter': '/usr/bin/python3',
            'ansible_ssh_private_key_file': str(root / 'credential'),
            'literal_path': str(root / 'must-not-rewrite'),
        }}}}))
        (root / 'credential').write_text('test-credential')
        (root / 'project').mkdir()
        return dict(private_data_dir=str(root), inventory=str(inventory), host_pattern='all')

    def test_adhoc_and_failed_playbook_return_events_and_rc(self):
        with tempfile.TemporaryDirectory() as root:
            events = []
            result = run(**self.options, **self.prepare(root), module='command', module_args='id -u',
                         event_handler=events.append)
            self.assertEqual((result.status, result.rc), ('successful', 0))
            self.assertTrue(any(e.get('event_data', {}).get('res', {}).get('stdout') == '100010'
                                for e in events))
            self.assertTrue(all('pid' not in e for e in events))
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            (Path(root) / 'project/main.yml').write_text(json.dumps([{
                'hosts': 'all', 'gather_facts': False,
                'tasks': [{'ansible.builtin.command': '/bin/false'}],
            }]))
            result = run(**self.options, **options, playbook='main.yml')
            self.assertEqual((result.status, result.rc), ('failed', 2))

    def test_upload_and_custom_modules_relocate_only_execution_paths(self):
        with tempfile.TemporaryDirectory(prefix='jms source ') as root:
            options = self.prepare(root)
            (Path(root) / 'upload').mkdir()
            (Path(root) / 'upload/file.txt').write_text('uploaded')
            events = []
            result = run(**self.options, **options, module='copy', module_args=(
                f'src={shlex.quote(root + "/upload/")} dest=/home/jms-executor/jms_10/uploaded/'),
                         event_handler=events.append)
            self.assertEqual(result.rc, 0, events)
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            modules = Path(root) / 'custom_modules'
            modules.mkdir()
            (modules / 'check_paths.py').write_text('''#!/usr/bin/python3
from ansible.module_utils.basic import AnsibleModule
from pathlib import Path
m = AnsibleModule(argument_spec=dict(key=dict(required=True), literal=dict(required=True)))
assert Path(m.params['key']).read_text() == 'test-credential'
assert m.params['literal'].startswith('SOURCE_ROOT/')
m.exit_json(changed=False, msg='CUSTOM_OK')
'''.replace('SOURCE_ROOT', root))
            (Path(root) / 'project/main.yml').write_text(json.dumps([{
                'hosts': 'all', 'gather_facts': False, 'tasks': [{'check_paths': {
                    'key': '{{ ansible_ssh_private_key_file }}', 'literal': '{{ literal_path }}',
                }}],
            }]))
            events = []
            result = run(**self.options, **options, playbook='main.yml',
                         envvars={'ANSIBLE_LIBRARY': str(modules)}, event_handler=events.append)
            self.assertEqual(result.rc, 0, events)
            self.assertTrue(any(e.get('event_data', {}).get('res', {}).get('msg') == 'CUSTOM_OK'
                                for e in events))

    def test_streaming_cancellation_timeout_and_uid_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            cancel = threading.Event()
            caller = threading.get_ident()
            def event_handler(event):
                self.assertEqual(threading.get_ident(), caller)
                if event['event'] == 'runner_on_start':
                    cancel.set()
            result = run(**self.options, **options, module='command', module_args='sleep 60',
                         event_handler=event_handler, cancel_callback=cancel.is_set)
            self.assertEqual(result.status, 'canceled')
        Fixture.wait_removed('/home/jms-executor/jms_10')
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            result = run(**self.options, **options, module='command', module_args='sleep 60',
                         job_timeout=1.5)
            self.assertEqual(result.status, 'timeout')
        Fixture.wait_removed('/home/jms-executor/jms_10')
        self.assertEqual(Fixture.ssh('jms_10', 'jms-executor-info').returncode, 0)

    def test_wrong_credentials_and_host_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            with self.assertRaises(paramiko.AuthenticationException):
                run(**(self.options | {'private_key': Fixture.wrong_key}), **options,
                    module='ping')
            wrong_hosts = Path(root) / 'wrong_known_hosts'
            wrong_hosts.write_text(f'[127.0.0.1]:{self.options["port"]} '
                                   + Path(str(Fixture.wrong_key) + '.pub').read_text())
            with self.assertRaises(paramiko.BadHostKeyException):
                run(**(self.options | {'known_hosts': wrong_hosts}), **options, module='ping')


if __name__ == '__main__':
    unittest.main(defaultTest='SDKTest', verbosity=2)
