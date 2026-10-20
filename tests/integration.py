"""Run against a real sshd in an ephemeral Linux container; stdlib-only client.

Build tests/Dockerfile first, then run this file with Python 3 on the Docker host.
All keys, containers, job inputs and outputs created here are test-only.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid


IMAGE = os.environ.get('JMS_EXECUTOR_TEST_IMAGE', 'jumpserver-executor-ssh:test')


def run(*args, input=None, check=True):
    return subprocess.run(args, input=input, capture_output=True, check=check, timeout=90)


class ExecutorSSHTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='jms-executor-ssh-')
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.key = root / 'client'
        cls.wrong_key = root / 'wrong-client'
        for key in (cls.key, cls.wrong_key):
            run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key))
        cls.name = 'jms-executor-test-' + uuid.uuid4().hex[:12]
        run(
            'docker', 'run', '-d', '--name', cls.name, '--security-opt', 'no-new-privileges:true',
            '--pids-limit', '256', '--memory', '1g',
            '--tmpfs', '/run', '--tmpfs', '/home/jms-executor', '--tmpfs', '/var/lib/jms-executor',
            '--mount', f'type=bind,src={cls.key}.pub,dst=/run/secrets/jms-executor.pub,readonly',
            '-e', 'EXECUTOR_SERVICE_SECRET=must-not-reach-jobs',
            '-p', '127.0.0.1::2222', IMAGE,
        )
        cls.addClassCleanup(lambda: run('docker', 'rm', '-f', cls.name, check=False))
        published = run('docker', 'port', cls.name, '2222/tcp', check=False)
        if published.returncode:
            raise RuntimeError(published.stderr.decode() + run('docker', 'logs', cls.name).stderr.decode())
        port = published.stdout.decode().strip().split(':')[-1]
        cls.known_hosts = root / 'known_hosts'
        # Obtain the test server's host key over Docker's trusted control channel,
        # not by accepting an unauthenticated ssh-keyscan result.
        for attempt in range(50):
            state = run('docker', 'inspect', '-f', '{{.State.Running}}', cls.name).stdout.strip()
            if state != b'true':
                raise RuntimeError(run('docker', 'logs', cls.name).stderr.decode())
            public = cls.container('cat', '/var/lib/jms-executor/ssh_host_ed25519_key.pub', check=False)
            if public.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError('Executor did not generate its host key')
        cls.known_hosts.write_text(f'[127.0.0.1]:{port} {public.stdout.decode()}')
        cls.ssh_options = [
            'ssh', '-p', port, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={cls.known_hosts}',
            '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR',
        ]
        for attempt in range(50):
            ready = cls.ssh('jms_1', 'jms-executor-info', check=False)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            logs = run('docker', 'logs', cls.name)
            raise RuntimeError(ready.stderr.decode() + logs.stderr.decode())

    @classmethod
    def container(cls, *args, input=None, check=True):
        return run('docker', 'exec', '-i', cls.name, *args, input=input, check=check)

    @classmethod
    def ssh(cls, user, command, *, key=None, input=None, check=True, options=()):
        return run(
            *cls.ssh_options, '-i', str(key or cls.key), *options,
            f'{user}@127.0.0.1', command, input=input, check=check,
        )

    @classmethod
    def job(cls, user, code, envvars=None):
        """Exercise the full transmit -> SSH worker -> process protocol."""
        job_id = uuid.uuid4().hex
        directory = '/tmp/jms-input-' + job_id
        result = '/tmp/jms-result-' + job_id
        playbook = [{
            'hosts': 'all', 'gather_facts': False,
            'tasks': [{'name': 'Verify task isolation', 'ansible.builtin.command': {
                'argv': ['/usr/bin/python3', '-c', code],
            }}],
        }]
        payload = {
            'project/main.yml': json.dumps(playbook),
            'inventory/hosts': json.dumps({'all': {'hosts': {'localhost': {
                'ansible_connection': 'local', 'ansible_python_interpreter': '/usr/bin/python3',
            }}}}),
            'env/envvars': json.dumps(envvars or {}),
        }
        create_inputs = (
            'import json, pathlib, sys; root=pathlib.Path(sys.argv[1]); '
            'files=json.load(sys.stdin); '
            '[( (root/p).parent.mkdir(parents=True, exist_ok=True), '
            '(root/p).write_text(value)) for p,value in files.items()]'
        )
        cls.container('/usr/bin/python3', '-c', create_inputs, directory,
                      input=json.dumps(payload).encode())
        try:
            stream = cls.container('/usr/bin/python3', '-I', '-m', 'ansible_runner',
                                   'transmit', directory, '-p', 'main.yml').stdout
            executed = cls.ssh(user, 'ansible-runner worker', input=stream, check=False)
            if not executed.stdout:
                raise RuntimeError(executed.stderr.decode())
            processed = cls.container('/usr/bin/python3', '-I', '-m', 'ansible_runner',
                                      'process', result, input=executed.stdout)
            read_result = (
                'import json,pathlib,sys; '
                'artifact=pathlib.Path(sys.argv[1])/"artifacts"; '
                'print(json.dumps({"rc":int((artifact/"rc").read_text()), '
                '"events":[json.loads(p.read_text()) '
                'for p in (artifact/"job_events").glob("*.json")]}))'
            )
            output = cls.container('/usr/bin/python3', '-c', read_result, result, check=False)
            if output.returncode:
                raise RuntimeError(output.stderr.decode() + executed.stderr.decode()
                                   + processed.stdout.decode(errors='replace'))
            return json.loads(output.stdout)
        finally:
            cls.container('rm', '-rf', directory, result)

    def test_shared_key_resolves_distinct_virtual_users_without_passwd_records(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda user: self.ssh(user, 'jms-executor-info'), ('jms_1', 'jms_2')))
        for index, response in enumerate(responses, 1):
            identity = json.loads(response.stdout)
            self.assertEqual(identity['uid'], 100000 + index)
            self.assertEqual(identity['gid'], identity['uid'])
            self.assertFalse(set(identity['groups']) - {identity['gid']})
        passwd = self.container('cat', '/etc/passwd').stdout
        self.assertNotIn(b'jms_1:', passwd)
        self.assertNotIn(b'jms_2:', passwd)

    def test_wrong_key_and_invalid_users_are_rejected_before_home_creation(self):
        result = self.ssh('jms_3', 'jms-executor-info', key=self.wrong_key, check=False)
        self.assertEqual(result.returncode, 255)
        self.assertNotEqual(self.container('test', '-e', '/home/jms-executor/jms_3', check=False).returncode, 0)
        for user in ('root', 'jms_0', 'jms_01', 'jms_65536'):
            with self.subTest(user=user):
                self.assertEqual(self.ssh(user, 'jms-executor-info', check=False).returncode, 255)

    def test_only_runner_protocol_and_probe_are_allowed(self):
        for command in ('', 'id', 'sh', 'ansible-runner worker; id', 'ansible-runner worker --help'):
            with self.subTest(command=command):
                self.assertNotEqual(self.ssh('jms_1', command, check=False).returncode, 0)
        forwarded = self.ssh('jms_1', '', options=('-W', '127.0.0.1:2222'), check=False)
        self.assertNotEqual(forwarded.returncode, 0)

    def test_real_ansible_jobs_cannot_read_other_uid_files_or_environment(self):
        self.ssh('jms_2', 'jms-executor-info')
        holder = (
            'import os,pathlib,time; home=pathlib.Path("/home/jms-executor/jms_2"); '
            '(home/"secret").write_text("user-b-secret"); '
            '(home/"holder.pid").write_text(str(os.getpid())); time.sleep(80)'
        )
        run('docker', 'exec', '-d', '-u', '100002:100002', '-e', 'B_SECRET=user-b-secret',
            self.name, '/usr/bin/python3', '-c', holder)
        pid_path = '/home/jms-executor/jms_2/holder.pid'
        for attempt in range(30):
            pid = self.container('cat', pid_path, check=False)
            if pid.returncode == 0:
                break
            time.sleep(0.1)
        self.assertEqual(pid.returncode, 0)
        pid = int(pid.stdout)
        try:
            code = f'''
import os, pathlib
assert os.getuid() == 100001
assert 'B_SECRET' not in os.environ and 'EXECUTOR_SERVICE_SECRET' not in os.environ
for path in ['/home/jms-executor/jms_2/secret', '/proc/{pid}/environ',
             '/var/lib/jms-executor/ssh_host_ed25519_key']:
    try:
        pathlib.Path(path).read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('Cross-user read succeeded: ' + path)
try:
    os.kill({pid}, 0)
except PermissionError:
    pass
else:
    raise AssertionError('Cross-user signal permission granted')
for path in ['/run/jms-executor/authorized_keys', '/usr/local/libexec/jms-executor-session']:
    assert not os.access(path, os.W_OK), path
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
pathlib.Path('owned-by-a').write_text('private')
assert pathlib.Path('owned-by-a').stat().st_uid == 100001
print('ISOLATION_OK')
'''
            result = self.job('jms_1', code)
            self.assertEqual(result['rc'], 0, json.dumps(result))
            self.assertTrue(any(
                event.get('event_data', {}).get('res', {}).get('stdout') == 'ISOLATION_OK'
                for event in result['events']
            ))
        finally:
            self.container('/usr/bin/python3', '-c', 'import os,sys; os.kill(int(sys.argv[1]),15)', str(pid))

    def test_task_environment_is_not_inherited_by_the_next_session(self):
        first = self.job('jms_4', 'import os; assert os.environ["JOB_SECRET"] == "first-only"',
                         {'JOB_SECRET': 'first-only'})
        self.assertEqual(first['rc'], 0, json.dumps(first))
        second = self.job('jms_4', 'import os; assert "JOB_SECRET" not in os.environ')
        self.assertEqual(second['rc'], 0, json.dumps(second))

    def test_symlinked_temporary_directory_is_rejected(self):
        self.ssh('jms_5', 'jms-executor-info')
        replace_temp = (
            'import pathlib; p=pathlib.Path("/home/jms-executor/jms_5/tmp"); '
            'p.rmdir(); p.symlink_to("/home/jms-executor/jms_2", target_is_directory=True)'
        )
        run('docker', 'exec', '-u', '100005:100005', self.name,
            '/usr/bin/python3', '-c', replace_temp)
        result = self.ssh('jms_5', 'jms-executor-info', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'private temporary directory', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
