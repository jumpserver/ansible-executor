"""Verify the actual publish steps against a local registry; needs Docker, crane and PyYAML."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen
import uuid

import yaml

source = Path(__file__).resolve().parents[1]
workflow = yaml.safe_load((source / '.github/workflows/build.yml').read_text())
push = next(s['run'] for s in workflow['jobs']['integration']['steps']
            if s.get('name') == 'Push the tested image by digest')
publish = next(s['run'] for s in workflow['jobs']['publish']['steps']
               if s.get('name') == 'Publish tested multi-architecture branch image')


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


with tempfile.TemporaryDirectory(prefix='jms-digest-publish-') as directory:
    root = Path(directory)
    tags = []
    registries = []
    addresses = []
    with (root / 'registry.log').open('w') as log:
        try:
            for _ in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    address = '127.0.0.1:' + str(sock.getsockname()[1])
                registries.append(subprocess.Popen(
                    ['crane', 'registry', 'serve', '--address', address], stdout=log, stderr=log))
                addresses.append(address)
                for _ in range(50):
                    try:
                        urlopen('http://' + address + '/v2/', timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError('Local registry did not start')
            repo, mirror = (address + '/executor' for address in addresses)
            (root / 'Dockerfile').write_text('FROM scratch\nCOPY payload /payload\n')
            (root / 'payload').write_text('digest-only publishing test\n')
            env = dict(os.environ, RUNNER_TEMP=directory, BRANCH_IMAGE=repo + ':master',
                       MIRROR_IMAGE=mirror + ':master',
                       GITHUB_STEP_SUMMARY=str(root / 'summary'), GITHUB_SHA='test-source')
            for arch in ('amd64', 'arm64'):
                tag = 'jms-digest-test-' + uuid.uuid4().hex + ':' + arch
                tags.append(tag)
                run('docker', 'build', '--platform', 'linux/' + arch, '-t', tag, directory)
                env.update(TEST_IMAGE=tag, ARCH=arch)
                run('bash', '-e', '-o', 'pipefail', '-c',
                    push.replace('docker.io/jumpserver/ansible-executor', repo), cwd=root, env=env)
                image = (root / 'image-digests' / arch).read_text().strip()
                assert image.startswith(repo + '@sha256:')
                assert json.loads(run('crane', 'config', image))['architecture'] == arch
                assert not run('crane', 'ls', repo), 'Digest upload created a tag'
                assert not (root / 'executor.tar').exists(), 'Archive not cleaned up'
            legacy_image = (root / 'image-digests' / 'amd64').read_text().strip()
            for registry_repo in (repo, mirror):
                run('crane', 'cp', legacy_image, registry_repo + ':latest')
            legacy_digest = run('crane', 'digest', repo + ':latest')
            digests = []
            for _ in range(2):
                run('bash', '-e', '-o', 'pipefail', '-c',
                    publish.replace('docker.io/jumpserver/ansible-executor', repo), cwd=root, env=env)
                image = (root / 'ansible-executor-image.txt').read_text().strip()
                digest = run('crane', 'digest', repo + ':master')
                assert image == repo + '@' + digest
                digests.append(digest)
                for registry_repo in (repo, mirror):
                    assert run('crane', 'digest', registry_repo + ':master') == digest
                    assert run('crane', 'digest', registry_repo + ':latest') == legacy_digest
                    assert sorted(run('crane', 'ls', registry_repo).splitlines()) == ['latest', 'master'], 'Unexpected public tag'
                    manifest = json.loads(run('crane', 'manifest', registry_repo + '@' + digest))
                    assert sorted(m['platform']['architecture'] for m in manifest['manifests']) == ['amd64', 'arm64']
                    for child in manifest['manifests']:
                        config = json.loads(run('crane', 'config', registry_repo + '@' + child['digest']))
                        assert config['architecture'] == child['platform']['architecture']
            assert digests[0] == digests[1], 'Retry changed the published image'
            print('PASS: both registries share the digest and architectures; latest is unchanged; retries create no extra tags')
        finally:
            for registry in registries:
                registry.terminate()
                registry.wait(timeout=10)
            for tag in tags:
                subprocess.run(['docker', 'image', 'rm', tag], capture_output=True)
