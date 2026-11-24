import errno
import importlib.util
from pathlib import Path
import signal
import unittest
from unittest.mock import call, patch


spec = importlib.util.spec_from_file_location('executor_cleanup', Path(__file__).parents[1] / 'cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def test_native_pidfd_open_is_used_when_available(self):
        with patch.object(cleanup.os, 'pidfd_open', create=True, return_value=42) as native, \
                patch.object(cleanup.os, 'open') as proc:
            self.assertEqual(cleanup.open_pidfd(123), 42)
        native.assert_called_once_with(123)
        proc.assert_not_called()

    def test_enosys_uses_a_proc_directory_handle(self):
        with patch.object(cleanup.os, 'pidfd_open', create=True, side_effect=OSError(errno.ENOSYS, 'unsupported')), \
                patch.object(cleanup.os, 'open', return_value=42) as proc:
            self.assertEqual(cleanup.open_pidfd(123), 42)
        proc.assert_called_once_with('/proc/123', cleanup.os.O_RDONLY | cleanup.os.O_DIRECTORY
                                     | cleanup.os.O_CLOEXEC | cleanup.os.O_NOFOLLOW)

    def test_permission_and_resource_errors_do_not_enable_the_fallback(self):
        for error in (errno.EPERM, errno.EMFILE):
            with self.subTest(error=error), \
                    patch.object(cleanup.os, 'pidfd_open', create=True, side_effect=OSError(error, 'failure')), \
                    patch.object(cleanup.os, 'open') as proc:
                with self.assertRaises(OSError) as raised:
                    cleanup.open_pidfd(123)
                self.assertEqual(raised.exception.errno, error)
                proc.assert_not_called()

    def run_cleanup(self, *, survives=False, failure=None):
        target, other, zombie = (Path('/proc') / str(pid) for pid in (123, 456, 789))
        statuses = {
            target / 'status': 'Uid:\t100001\t100001\t100001\t100001\nState:\tS\n',
            other / 'status': 'Uid:\t100002\t100002\t100002\t100002\nState:\tS\n',
            zombie / 'status': 'Uid:\t100001\t100001\t100001\t100001\nState:\tZ\n',
        }
        with patch.object(cleanup.os, 'geteuid', return_value=0), \
                patch.object(cleanup, 'open_pidfd', return_value=42), \
                patch.object(cleanup.os, 'close') as close, \
                patch.object(Path, 'iterdir', side_effect=[[target, other, zombie], []]), \
                patch.object(Path, 'read_text', autospec=True, side_effect=lambda path: statuses[path]), \
                patch.object(Path, 'exists', return_value=True), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(cleanup.shutil, 'rmtree') as remove_home, \
                patch.object(cleanup.signal, 'pidfd_send_signal', create=True, side_effect=failure) as send, \
                patch.object(cleanup, 'clean_shared') as shared, \
                patch.object(cleanup.time, 'sleep'), \
                patch.object(cleanup.time, 'monotonic', side_effect=[0, 6] if survives else [0, 1, 2]):
            if survives or failure:
                with self.assertRaises(OSError if failure else RuntimeError):
                    cleanup.clean(100001)
                remove_home.assert_not_called()
                shared.assert_not_called()
            else:
                cleanup.clean(100001)
                remove_home.assert_called_once_with(Path('/home/jms-executor/jms_1'))
                shared.assert_called_once()
            if failure:
                send.assert_called_once_with(42, 0)
                close.assert_called_once_with(42)
            else:
                self.assertEqual(send.call_args_list, [call(42, 0), call(42, signal.SIGKILL)])
                self.assertEqual(close.call_count, 4)

    def test_cleanup_signals_only_live_processes_of_the_leased_uid(self):
        self.run_cleanup()

    def test_survivors_and_missing_signal_support_prevent_file_cleanup(self):
        self.run_cleanup(survives=True)
        self.run_cleanup(failure=OSError(errno.ENOSYS, 'pidfd_send_signal unavailable'))
        self.run_cleanup(failure=PermissionError(errno.EPERM, 'pidfd_send_signal denied'))


if __name__ == '__main__':
    unittest.main()
