"""Relocate only execution paths; never substitute inside arbitrary user values."""
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import ansible_runner
from ansible_runner.streaming import Worker
from ansible_runner.utils.streaming import unstream_dir
from .protocol import json_line, MAX_ARCHIVE

PATH_FIELDS = {
    'ansible_ssh_private_key_file', 'ansible_private_key_file', 'private_key_path',
    'jms_custom_become_private_key_path', 'ca_cert', 'client_cert', 'client_key',
    'client_cert_key',
}
ENV_PATHS = {
    'ANSIBLE_CONFIG', 'ANSIBLE_LIBRARY', 'PYTHONPATH', 'ANSIBLE_ROLES_PATH',
    'ANSIBLE_SSH_EXECUTABLE', 'ANSIBLE_SCP_EXECUTABLE', 'ANSIBLE_SFTP_EXECUTABLE',
}


class RelocatingWorker(Worker):
    def __init__(self, source_dir=None, **kwargs):
        self.source_dir = source_dir
        super().__init__(**kwargs)

    def run(self):
        self._begin_keepalive()
        try:
            options = None
            while True:
                message = json_line(self._input)
                if 'kwargs' in message:
                    if options is not None or not isinstance(message['kwargs'], dict):
                        raise ValueError('Invalid task options')
                    options = message['kwargs']
                elif 'zipfile' in message:
                    length = message['zipfile']
                    if type(length) is not int or not 0 < length <= MAX_ARCHIVE:
                        raise ValueError('Invalid task archive')
                    unstream_dir(self._input, length, self.private_data_dir)
                elif message.get('eof') is True:
                    break
                else:
                    raise ValueError('Invalid task stream')
            if options is None:
                raise ValueError('Missing task options')
            options = self.update_paths(options)
            options.update(private_data_dir=self.private_data_dir, quiet=True,
                           suppress_ansible_output=True, event_handler=self.event_handler,
                           status_handler=self.status_handler, artifacts_handler=self.artifacts_handler,
                           finished_callback=self.finished_callback)
            runner = ansible_runner.run(**options)
            self.status, self.rc = runner.status, runner.rc
            return self.status, self.rc
        finally:
            self._end_keepalive()

    def relocate(self, value):
        if not isinstance(value, str) or not self.source_dir:
            return value
        if value == self.source_dir or value.startswith(self.source_dir + '/'):
            return self.private_data_dir + value[len(self.source_dir):]
        return value

    def inventory_paths(self, value):
        if isinstance(value, dict):
            return {key: self.relocate(item) if key in PATH_FIELDS else self.inventory_paths(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.inventory_paths(item) for item in value]
        return value

    def update_paths(self, kwargs):
        # Every received task remains under the UID selected by sshd; no nested runtime.
        kwargs['process_isolation'] = False
        for key in list(kwargs):
            if key.startswith('container_') or key.startswith('process_isolation_'):
                kwargs.pop(key)
        kwargs.pop('directory_isolation_base_path', None)
        kwargs.pop('artifact_dir', None)
        root = Path(self.private_data_dir)
        (root / 'project').mkdir(mode=0o700, exist_ok=True)
        kwargs['project_dir'] = str(root / 'project')
        kwargs['host_cwd'] = str(root / 'project')
        env = kwargs.setdefault('envvars', {})
        for key in ENV_PATHS & env.keys():
            env[key] = os.pathsep.join(self.relocate(part) for part in env[key].split(os.pathsep))
        # Transmitter.dump_artifacts normally moves envvars into env/envvars.
        env_file = root / 'env' / 'envvars'
        if env_file.exists():
            stored_env = json.loads(env_file.read_text())
            for key in ENV_PATHS & stored_env.keys():
                stored_env[key] = os.pathsep.join(
                    self.relocate(part) for part in stored_env[key].split(os.pathsep))
            env_file.write_text(json.dumps(stored_env))
            env_file.chmod(0o600)
        if kwargs.get('inventory'):
            inventory = Path(kwargs['inventory'])
            if not inventory.is_absolute():
                inventory = root / inventory
            inventory = Path(self.relocate(str(inventory)))
            if not inventory.resolve().is_relative_to(root.resolve()):
                raise ValueError('Inventory must reside inside the task directory')
            # JumpServer generates JSON inventories. Text inventories remain valid
            # for generic users, but cannot contain absolute credential references.
            try:
                value = json.loads(inventory.read_text())
            except ValueError:
                pass
            else:
                inventory.write_text(json.dumps(self.inventory_paths(value)))
                inventory.chmod(0o600)
            kwargs['inventory'] = str(inventory)
        if kwargs.get('module') == 'copy' and isinstance(kwargs.get('module_args'), str):
            parts = shlex.split(kwargs['module_args'])
            arguments = []
            for part in parts:
                key, separator, value = part.partition('=')
                if not separator:
                    raise ValueError('copy requires key=value arguments')
                if key == 'src':
                    value = self.relocate(value)
                # Ansible's key=value parser requires quoting the value only.
                arguments.append(key + '=' + shlex.quote(value))
            kwargs['module_args'] = ' '.join(arguments)
        return kwargs


def main():
    managed = len(sys.argv) == 2
    if managed:
        job_id = str(uuid.UUID(sys.argv[1]))
        print(json.dumps({'executor_protocol': 1, 'job_id': job_id, 'uid': os.getuid()}), flush=True)
        metadata = json_line(sys.stdin.buffer)
        source_dir = metadata.get('source_dir')
        if not isinstance(source_dir, str) or not source_dir.startswith('/') or '\x00' in source_dir:
            raise ValueError('Invalid source task directory')
        source_dir = source_dir.rstrip('/')
    else:
        source_dir = None
    worker = RelocatingWorker(source_dir=source_dir, keepalive_seconds=10)
    worker.run()
    return worker.rc if worker.rc is not None else 1


if __name__ == '__main__':
    sys.exit(main())
