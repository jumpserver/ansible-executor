"""Check branch and release tags used for publishing."""
from pathlib import Path
import runpy
import unittest


image_tags = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / '.github/image_tag.py')
)
branch_tag = image_tags['branch_tag']
release_tag = image_tags['release_tag']


class ImageTagTest(unittest.TestCase):
    def test_master_keeps_jenkins_source_tag(self):
        self.assertEqual(branch_tag('master'), 'master')

    def test_branch_tags(self):
        for branch, tag in (
            ('dev', 'dev'), ('v5', 'v5'),
            ('feature/runtime', 'feature-runtime'), ('.runtime', '_runtime'),
        ):
            with self.subTest(branch=branch):
                self.assertEqual(branch_tag(branch), tag)

    def test_reserved_tags_are_rejected(self):
        for branch in ('latest', 'LATEST', 'build-123-1', 'v5.1.0',
                       '5.1.0', 'v5.1.0-rc.1', 'v5.1.0-ce'):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                branch_tag(branch)

    def test_invalid_lengths_are_rejected(self):
        self.assertEqual(branch_tag('x' * 128), 'x' * 128)
        for branch in ('', 'x' * 129):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                branch_tag(branch)

    def test_release_tags_preserve_the_git_tag(self):
        for tag in ('v0.1.2', 'v0.1.2-rc.1', 'v0.1.2-' + 'x' * 121):
            with self.subTest(tag=tag):
                self.assertEqual(release_tag(tag), tag)

    def test_invalid_release_tags_are_rejected(self):
        for tag in ('master', 'latest', 'v5', '0.1.2', 'v0.1.2-', 'v0.1.2/rc.1',
                    'v0.1.2-' + 'x' * 122):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_tag(tag)
