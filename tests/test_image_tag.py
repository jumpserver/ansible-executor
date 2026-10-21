"""Keep branch publishing away from legacy, release and candidate tags."""
from pathlib import Path
import runpy
import unittest


branch_tag = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / '.github/image_tag.py')
)['branch_tag']


class ImageTagTest(unittest.TestCase):
    def test_branch_tags(self):
        for branch, tag in (
            ('master', 'master'), ('dev', 'dev'), ('v5', 'v5'),
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
