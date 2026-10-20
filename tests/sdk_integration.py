"""Real SDK and upstream/fork Runner interoperability checks."""
from concurrent.futures import ThreadPoolExecutor
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
        inventory.write_text(json.dumps({'all': {'hosts': {'executor_test': {
            'ansible_connection': 'local', 'ansible_python_interpreter': '/usr/bin/python3',
            'ansible_ssh_private_key_file': str(root / 'credential'),
            'literal_path': str(root / 'must-not-rewrite'),
        }}}}))
        (root / 'credential').write_text('test-credential')
        (root / 'project').mkdir()
        # These trusted localhost fixtures use Core's system automation mode.
        return dict(private_data_dir=str(root), inventory=str(inventory), host_pattern='all',
                    envvars={'ANSIBLE_SUPER_MODE': '1'})

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
            options['envvars']['ANSIBLE_LIBRARY'] = str(modules)
            result = run(**self.options, **options, playbook='main.yml',
                         event_handler=events.append)
            self.assertEqual(result.rc, 0, events)
            self.assertTrue(any(e.get('event_data', {}).get('res', {}).get('msg') == 'CUSTOM_OK'
                                for e in events))

    def test_business_drivers_and_collections_available_to_task(self):
        code = '''
import importlib
import json
import subprocess
for name in ('boto3', 'botocore', 's3transfer', 'paramiko', 'passlib', 'sshtunnel',
             'winrm', 'telnetlib3', 'MySQLdb', 'psycopg2', 'pymssql', 'pymongo',
             'oracledb', 'pyfreerdp'):
    importlib.import_module(name)
from passlib.hash import bcrypt
assert bcrypt.verify('runtime-check', bcrypt.hash('runtime-check'))
modules = ('ansible.windows.win_ping', 'community.mysql.mysql_info',
           'community.postgresql.postgresql_ping', 'amazon.aws.ec2_instance_info')
docs = subprocess.run(['ansible-doc', '--json', *modules],
                      check=True, capture_output=True, text=True)
assert set(modules) <= json.loads(docs.stdout).keys()
print('RUNTIME_DEPENDENCIES_OK')
'''
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            events = []
            result = run(**self.options, **options, module='command',
                         module_args='/usr/bin/python3 -c ' + shlex.quote(code),
                         event_handler=events.append)
            self.assertEqual(result.rc, 0, events)
            self.assertTrue(any(e.get('event_data', {}).get('res', {}).get('stdout')
                                == 'RUNTIME_DEPENDENCIES_OK' for e in events), events)

    def test_task_ssh_wrappers_are_executable_after_transfer(self):
        with tempfile.TemporaryDirectory(prefix='jms source ') as root:
            options = self.prepare(root)
            wrappers = Path(root) / '.serialized-ssh'
            wrappers.mkdir(mode=0o700)
            for client in ('ssh', 'scp', 'sftp'):
                wrapper = wrappers / client
                wrapper.write_text('#!/bin/sh\nprintf WRAPPER_OK\n')
                wrapper.chmod(0o700)
                options['envvars'][f'ANSIBLE_{client.upper()}_EXECUTABLE'] = str(wrapper)
            code = '''
import os
import subprocess
for client in ('SSH', 'SCP', 'SFTP'):
    wrapper = os.environ[f'ANSIBLE_{client}_EXECUTABLE']
    result = subprocess.run(['sshpass', '-p', 'test-only', wrapper],
                            capture_output=True, text=True)
    assert result.returncode == 0, (wrapper, result.stderr)
    assert result.stdout == 'WRAPPER_OK'
print('SSH_WRAPPERS_OK')
'''
            events = []
            result = run(**self.options, **options, module='command',
                         module_args='/usr/bin/python3 -c ' + shlex.quote(code),
                         event_handler=events.append)
            self.assertEqual(result.rc, 0, events)
            self.assertTrue(any(e.get('event_data', {}).get('res', {}).get('stdout')
                                == 'SSH_WRAPPERS_OK' for e in events), events)

    def test_gateway_password_file_is_not_exposed_to_another_uid(self):
        password = "test-only 'quoted' 50%% password"
        with tempfile.TemporaryDirectory(prefix="gateway '50% ") as root:
            options = self.prepare(root)
            credential = Path(root) / '.gateway-password'
            credential.write_text(password)
            credential.chmod(0o600)
            # A real sshpass/TTY exchange, held open so another UID can inspect /proc.
            prompt = (
                "import os,pathlib,termios,time; fd=os.open('/dev/tty',os.O_RDWR); "
                "attrs=termios.tcgetattr(fd); attrs[3]&=~termios.ECHO; "
                "termios.tcsetattr(fd,termios.TCSANOW,attrs); "
                "os.write(fd,b'Password: '); value=os.read(fd,4096).decode().rstrip('\\n'); "
                "assert value==pathlib.Path('.gateway-password').read_text(); "
                "pathlib.Path('.authenticated').touch(); time.sleep(30)"
            )
            command = ['sshpass', '-f', str(credential).replace('%', '%%'),
                       '/usr/bin/python3', '-c', prompt]
            inventory = Path(options['inventory'])
            hosts = json.loads(inventory.read_text())
            hosts['all']['hosts']['executor_test']['ansible_ssh_common_args'] = shlex.join(
                ['-o', 'ProxyCommand=' + shlex.join(command)])
            inventory.write_text(json.dumps(hosts))
            (Path(root) / 'project/victim.py').write_text('''
import json, pathlib, shlex, subprocess
root = pathlib.Path(__file__).resolve().parent.parent
host = json.loads((root / 'inventory/hosts').read_text())['all']['hosts']['executor_test']
command = shlex.split(shlex.split(host['ansible_ssh_common_args'])[1].split('=', 1)[1])
subprocess.run(command, cwd=root, check=True)
''')
            (Path(root) / 'project/main.yml').write_text(json.dumps([{
                'hosts': 'all', 'gather_facts': False, 'tasks': [{'ansible.builtin.command': {
                    'argv': ['/usr/bin/python3', '{{ playbook_dir }}/victim.py'],
                }}],
            }]))
            observer = '''
import json, pathlib, sys
secret = sys.stdin.buffer.read()
credential = None
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        if path.stat().st_uid != 100010:
            continue
        command = path.read_bytes()
    except (OSError, ProcessLookupError):
        continue
    assert secret not in command, 'Gateway password leaked in a process argument'
    args = command.split(b'\\0')
    if len(args) > 2 and args[:2] == [b'sshpass', b'-f']:
        credential = pathlib.Path(args[2].decode())
if credential is None:
    sys.exit(3)
try:
    credential.read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('Another UID read the gateway password file')
print(json.dumps({'credential': str(credential), 'isolated': True}))
'''
            canceled = threading.Event()
            events = []
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run, **self.options, **options, playbook='main.yml',
                                     cancel_callback=canceled.is_set, event_handler=events.append)
                try:
                    for _ in range(100):
                        checked = Fixture.container(
                            'runuser', '-u', 'jms_11', '--', '/usr/bin/python3', '-c', observer,
                            input=password.encode(), check=False)
                        if checked.returncode != 3:
                            break
                        time.sleep(0.1)
                    self.assertEqual(checked.returncode, 0, checked.stderr.decode() + str([
                        event.get('event_data', {}).get('res')
                        for event in events if event['event'] == 'runner_on_failed'
                    ]))
                    credential_path = json.loads(checked.stdout)['credential']
                    self.assertTrue(credential_path.startswith('/home/jms-executor/jms_10/'))
                    for _ in range(50):
                        ready = Fixture.container(
                            'test', '-f', str(Path(credential_path).parent / '.authenticated'), check=False)
                        if ready.returncode == 0:
                            break
                        time.sleep(0.1)
                    self.assertEqual(ready.returncode, 0, 'sshpass did not authenticate using the file')
                finally:
                    canceled.set()
                self.assertEqual(future.result(timeout=15).status, 'canceled')
            Fixture.wait_removed('/home/jms-executor/jms_10')

    def test_streaming_cancellation_timeout_and_uid_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            options = self.prepare(root)
            (Path(root) / 'project/main.yml').write_text(json.dumps([{
                'hosts': 'all', 'gather_facts': False, 'tasks': [
                    {'ansible.builtin.debug': {'msg': 'LIVE_LOG_BEFORE_STOP'}},
                    {'name': 'Wait for cancellation', 'ansible.builtin.command': 'sleep 60'},
                ],
            }]))
            cancel = threading.Event()
            caller = threading.get_ident()
            output = []
            def event_handler(event):
                self.assertEqual(threading.get_ident(), caller)
                if event.get('stdout'):
                    output.append(event['stdout'])
                if (event['event'] == 'runner_on_start'
                        and event.get('event_data', {}).get('task') == 'Wait for cancellation'):
                    self.assertIn('LIVE_LOG_BEFORE_STOP', '\n'.join(output))
                    cancel.set()
            started = time.monotonic()
            result = run(**self.options, **options, playbook='main.yml',
                         event_handler=event_handler, cancel_callback=cancel.is_set)
            self.assertEqual(result.status, 'canceled')
            self.assertLess(time.monotonic() - started, 30)
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
