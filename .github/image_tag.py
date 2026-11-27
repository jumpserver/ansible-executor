"""Select the Docker image tag for a branch or an executor release."""
import os
import re


def branch_tag(branch):
    tag = re.sub(r'[^a-zA-Z0-9_.-]+', '-', branch)
    tag = re.sub(r'^[.-]+', '_', tag)
    if not tag or len(tag) > 128:
        raise ValueError('Branch name must produce a Docker tag of 1–128 characters')
    if (tag.lower() == 'latest' or tag.startswith('build-')
            or re.fullmatch(r'v?\d+\.\d+\.\d+(?:[.-].*)?', tag)):
        raise ValueError(f'Reserved image tag: {tag}')
    return tag


def release_tag(tag):
    pattern = r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9][a-zA-Z0-9_.-]*)?'
    if len(tag) > 128 or not re.fullmatch(pattern, tag):
        raise ValueError(f'Expected an executor version tag such as v0.1.2 or v0.1.2-rc.1: {tag}')
    return tag


if __name__ == '__main__':
    ref_name = os.environ['GITHUB_REF_NAME']
    ref_type = os.environ.get('GITHUB_REF_TYPE')
    print(release_tag(ref_name) if ref_type == 'tag' else branch_tag(ref_name))
