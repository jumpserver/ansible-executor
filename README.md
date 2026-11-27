# JumpServer Ansible Executor

English · [简体中文](README.zh-hans.md)

Run JumpServer Ansible jobs over SSH in a dedicated, long-running container.
Each job runs under a temporary Linux UID. Celery can run jobs concurrently
and receives live events and exit codes.

## FAQ

### Which image should I use?

For a release, use `jumpserver/ansible-executor:<version>` with the Core version
number, omitting any `-ce` or `-ee` suffix. For development, use the matching
branch tag. `latest` is reserved for older Core releases that start a container
per job.

### How are jobs isolated?

Each SSH session gets a private home directory and exclusive use of the non-root
UID associated with its SSH username (`jms_1` through `jms_65535`). The executor
cleans up job processes and files before reusing a UID. Jobs share the container's
network, process namespace, and resource limits. Keep secrets out of command
lines because process metadata can be visible across UIDs.

### What can a job upload?

Keep playbooks, inventory, modules, and job credentials in the job's private
data directory, using regular files and directories. Inputs are limited to
32 GiB, and returned artifacts to 128 MiB compressed and 512 MiB extracted.
Allow disk space for both archives and extracted files across concurrent jobs.

## Quick start

Run these commands from the repository root with Docker and an OpenSSH client.
The SSH daemon starts as root inside the container and drops privileges for jobs.
Session cleanup requires PID file descriptor signaling (`pidfd_send_signal`)
from the Linux kernel and container policy.

### 1. Build and start the executor

```sh
docker build -t jumpserver/ansible-executor:local .

mkdir -m 700 executor-secrets
ssh-keygen -q -t ed25519 -N '' -f executor-secrets/client

docker run -d --name jms-ansible-executor \
  --security-opt no-new-privileges:true \
  --memory 1g --pids-limit 256 \
  --tmpfs /run --tmpfs /home/jms-executor:exec,nosuid,nodev \
  --mount type=volume,src=jms-executor-host-key,dst=/var/lib/jms-executor \
  --mount "type=bind,src=$PWD/executor-secrets/client.pub,dst=/run/secrets/jms-executor.pub,readonly" \
  -p 127.0.0.1:2222:2222 \
  jumpserver/ansible-executor:local
```

The volume preserves the server's SSH host key across container replacements.
Keep `exec` on the home tmpfs so jobs can run uploaded scripts.

### 2. Verify the SSH connection

Read the host key directly from the running container, then check the execution
identity over SSH.

```sh
docker exec jms-ansible-executor cat /var/lib/jms-executor/ssh_host_ed25519_key.pub \
  | sed 's/^/[127.0.0.1]:2222 /' > executor-secrets/known_hosts

ssh -T -p 2222 -i executor-secrets/client \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=executor-secrets/known_hosts \
  jms_1@127.0.0.1 jms-executor-info
```

The command returns JSON with `uid` and `gid` both set to `100001`.

## Connect JumpServer Core

Standard Installer deployments already set the connection defaults. For a custom
deployment, put Celery and the executor on a private network, keep port 2222
internal, and ensure the executor can reach the target assets. Use a dedicated
service key per deployment. The key authorizes every executor UID, so give the
private key only to trusted Core/Celery services and mount only the public key
in the executor.

Mount the private key and trusted `known_hosts` file into Core/Celery at the
paths set in `config.yml`. The host key entry must use the actual endpoint,
for example `[ansible-executor]:2222`.

```yaml
ANSIBLE_DOCKER_ENABLED: true
ANSIBLE_EXECUTOR_HOST: ansible-executor
ANSIBLE_EXECUTOR_PORT: 2222
ANSIBLE_EXECUTOR_PRIVATE_KEY: /run/jms-executor/client
ANSIBLE_EXECUTOR_KNOWN_HOSTS: /run/jms-executor/known_hosts
```

For domain SSH tunnels, set `ANSIBLE_EXECUTOR_GATEWAY_HOST` to a Core/Celery
address reachable from the executor. See the [Core SSH backend guide](https://github.com/jumpserver/jumpserver/blob/dev/utils/ansible_executor/ssh-backend.md).

## Development

The image bundles the JumpServer Ansible and Runner forks, collections, and
drivers listed in [requirements-runtime.txt](requirements-runtime.txt). Core
ships its custom modules with each job.

The `jms-ansible-executor` Python SDK exposes [`client.run()`](src/jms_ansible_executor/client.py)
for submitting jobs. Use Python 3.11+ in a virtual environment for local checks.

```sh
python3 -m pip install .
python3 -m unittest discover -s tests -p 'test_*.py' -v
docker build --target test -t jumpserver-executor-ssh:test .
python3 tests/integration.py
python3 tests/sdk_integration.py
```

Image publishing and integration checks are defined in the
[build workflow](.github/workflows/build.yml).

## License

Copyright (c) 2014-2026 FIT2CLOUD, All rights reserved.

Licensed under the GNU General Public License version 3 (GPLv3), consistent with the [JumpServer main repository](https://github.com/jumpserver/jumpserver). See [LICENSE](LICENSE) for the full license text.
