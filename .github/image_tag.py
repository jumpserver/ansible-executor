"""Select the mutable branch tag without overwriting legacy or release images."""
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


if __name__ == '__main__':
    print(branch_tag(os.environ['GITHUB_REF_NAME']))
