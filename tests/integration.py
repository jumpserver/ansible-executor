"""Run against a real sshd in an ephemeral Linux container; stdlib-only client.

Build the Dockerfile's test target, then run this file with Python 3 on the Docker host.
All keys, containers, job inputs and outputs created here are test-only.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    @contextmanager
    def lease(cls, user):
        process = subprocess.Popen(
            [*cls.ssh_options, '-i', str(cls.key), f'{user}@127.0.0.1',
             'jms-executor-run ' + str(uuid.uuid4())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            hello = json.loads(process.stdout.readline())
            assert hello['uid'] == 100000 + int(user[4:])
            yield process
        finally:
            process.terminate()
            process.communicate(timeout=10)

    @classmethod
    def wait_removed(cls, path):
        for _ in range(100):
            if cls.container('test', '-e', path, check=False).returncode:
                return
            time.sleep(0.05)
        raise AssertionError('Session cleanup did not remove ' + path)

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
            'inventory/hosts': json.dumps({'all': {'hosts': {'executor_test': {
                'ansible_connection': 'local', 'ansible_python_interpreter': '/usr/bin/python3',
            }}}}),
            # Core's fork permits localhost only for trusted system automation.
            'env/envvars': json.dumps({'ANSIBLE_SUPER_MODE': '1', **(envvars or {})}),
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
        with self.lease('jms_2'):
            self.check_cross_uid_isolation()

    def check_cross_uid_isolation(self):
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

    def test_stale_home_is_cleaned_without_following_symlinks(self):
        self.wait_removed('/home/jms-executor/jms_5')
        replace_temp = (
            'import pathlib; home=pathlib.Path("/home/jms-executor/jms_5"); '
            'home.mkdir(); (home/"tmp").symlink_to("/etc", target_is_directory=True)'
        )
        self.container('/usr/bin/python3', '-c', replace_temp)
        result = self.ssh('jms_5', 'jms-executor-info', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.container('test', '-f', '/etc/passwd').returncode, 0)
        self.wait_removed('/home/jms-executor/jms_5')

    def test_same_uid_cannot_open_two_sessions_and_disconnect_cleans_daemons(self):
        with self.lease('jms_8'):
            self.assertNotEqual(self.ssh('jms_8', 'jms-executor-info', check=False).returncode, 0)
            run('docker', 'exec', '-u', '100008:100008', self.name,
                '/usr/bin/python3', '-c',
                'import pathlib; p=pathlib.Path("/tmp/jms-shared-test"); p.mkdir(); '
                '(p/"secret").write_text("old-task"); (p/"link").symlink_to("/etc/passwd"); '
                'pathlib.Path("/dev/shm/jms-secret").write_text("old-task")')
            run('docker', 'exec', '-d', '-u', '100008:100008', self.name,
                '/usr/bin/python3', '-c',
                'import os,time,sys\nif os.fork(): sys.exit(0)\nos.setsid()\n'
                'open("/home/jms-executor/jms_8/daemon.pid","w").write(str(os.getpid()))\n'
                'time.sleep(80)')
            for _ in range(50):
                started = self.container('test', '-f', '/home/jms-executor/jms_8/daemon.pid', check=False)
                if started.returncode == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(started.returncode, 0)
        self.wait_removed('/home/jms-executor/jms_8')
        self.wait_removed('/tmp/jms-shared-test')
        self.wait_removed('/dev/shm/jms-secret')
        self.container('test', '-f', '/etc/passwd')
        alive = self.container('/usr/bin/python3', '-c',
            'import pathlib; print([p.name for p in pathlib.Path("/proc").iterdir() '
            'if p.name.isdecimal() and p.stat().st_uid == 100008 '
            'and "State:\\tZ" not in (p/"status").read_text()])')
        self.assertEqual(alive.stdout.strip(), b'[]')
        self.assertEqual(self.ssh('jms_8', 'jms-executor-info').returncode, 0)

    def test_orphan_process_is_killed_before_uid_reuse(self):
        self.container('/usr/bin/python3', '-c',
            'import pathlib,os; home=pathlib.Path("/home/jms-executor/jms_9"); '
            'home.mkdir(mode=0o700); os.chown(home,100009,100009)')
        run('docker', 'exec', '-d', '-u', '100009:100009', self.name,
            '/usr/bin/python3', '-c',
            'import os,pathlib,time,sys\nif os.fork(): sys.exit(0)\nos.setsid()\n'
            'pathlib.Path("/home/jms-executor/jms_9/orphan.pid").write_text(str(os.getpid()))\n'
            'time.sleep(80)')
        for _ in range(50):
            orphan = self.container('cat', '/home/jms-executor/jms_9/orphan.pid', check=False)
            if orphan.returncode == 0:
                break
            time.sleep(0.05)
        self.assertEqual(orphan.returncode, 0)
        self.assertEqual(self.ssh('jms_9', 'jms-executor-info').returncode, 0)
        status = self.container('cat', f'/proc/{int(orphan.stdout)}/status', check=False)
        self.assertTrue(status.returncode != 0 or b'State:\tZ' in status.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
