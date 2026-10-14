import sys
import os
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from studio.tts.jobs import JobQueue


class JobQueueTests(unittest.TestCase):
    def run_script(self, directory, source, **kwargs):
        script = Path(directory) / 'child.py'
        script.write_text(textwrap.dedent(source), encoding='utf-8')
        return JobQueue, [sys.executable, str(script)]

    def test_captures_lines_and_contains_callback_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, command = self.run_script(tmp, """
                print('first', flush=True)
                print('second', flush=True)
            """)
            seen = []
            jobs = JobQueue(max_log_lines=10)
            job = jobs.submit(command, cwd=tmp, on_line=lambda line: (seen.append(line), (_ for _ in ()).throw(ValueError('callback'))))
            result = job.wait(5)
            jobs.shutdown()
            self.assertEqual(result.returncode, 0)
            self.assertEqual(seen, ['first', 'second'])
            self.assertEqual(result.logs, ['first', 'second'])
            self.assertIsNone(result.error)

    def test_python_child_stdout_is_utf8_even_when_parent_requests_cp1252(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, command = self.run_script(tmp, """
                print('verifica\u00e7\u00e3o da dublagem', flush=True)
            """)
            with patch.dict(os.environ, {'PYTHONIOENCODING': 'cp1252'}, clear=False):
                jobs = JobQueue()
                result = jobs.submit(command, cwd=tmp).wait(5)
                jobs.shutdown()
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.logs, ['verifica\u00e7\u00e3o da dublagem'])

    def test_queued_job_can_be_cancelled_before_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'second-started'
            _, first = self.run_script(tmp, """
                import time
                print('running', flush=True)
                time.sleep(0.4)
            """)
            second_script = Path(tmp) / 'second.py'
            second_script.write_text(
                f"from pathlib import Path; Path({str(marker)!r}).touch()\n", encoding='utf-8'
            )
            jobs = JobQueue()
            first_job = jobs.submit(first, cwd=tmp)
            deadline = time.time() + 3
            while not jobs.busy and time.time() < deadline:
                time.sleep(0.01)
            queued = jobs.submit([sys.executable, str(second_script)], cwd=tmp)
            queued.cancel()
            first_job.wait(5)
            cancelled = queued.wait(5)
            jobs.shutdown()
            self.assertTrue(cancelled.cancelled)
            self.assertFalse(marker.exists())

    def test_active_cancel_and_timeout_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, command = self.run_script(tmp, """
                import time
                print('ready', flush=True)
                time.sleep(5)
            """)
            jobs = JobQueue()
            job = jobs.submit(command, cwd=tmp, timeout=3)
            deadline = time.time() + 3
            while not jobs.busy and time.time() < deadline:
                time.sleep(0.01)
            job.cancel()
            cancelled = job.wait(5)
            self.assertTrue(cancelled.cancelled)
            self.assertNotEqual(cancelled.returncode, 0)

            timeout_job = jobs.submit(command, cwd=tmp, timeout=0.15)
            timeout_result = timeout_job.wait(5)
            jobs.shutdown()
            self.assertTrue(timeout_result.timed_out)

    def test_log_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, command = self.run_script(tmp, """
                for i in range(100):
                    print(i, flush=True)
            """)
            jobs = JobQueue(max_log_lines=7)
            result = jobs.submit(command, cwd=tmp).wait(5)
            jobs.shutdown()
            self.assertEqual(len(result.logs), 7)
            self.assertEqual(result.logs[-1], '99')

    def test_second_child_starts_after_first_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_done = Path(tmp) / 'first-done'
            order_file = Path(tmp) / 'order'
            first_script = Path(tmp) / 'first.py'
            second_script = Path(tmp) / 'second.py'
            first_script.write_text(
                f"import time; time.sleep(0.2); open({str(first_done)!r}, 'w').close()\n",
                encoding='utf-8',
            )
            second_script.write_text(
                f"from pathlib import Path; Path({str(order_file)!r}).write_text(str(Path({str(first_done)!r}).exists()))\n",
                encoding='utf-8',
            )
            jobs = JobQueue()
            first = jobs.submit([sys.executable, str(first_script)], cwd=tmp)
            second = jobs.submit([sys.executable, str(second_script)], cwd=tmp)
            self.assertEqual(first.wait(5).returncode, 0)
            self.assertEqual(second.wait(5).returncode, 0)
            jobs.shutdown()
            self.assertEqual(order_file.read_text(encoding='utf-8'), 'True')

    def test_exclusive_submit_rejects_work_queued_from_another_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, first = self.run_script(tmp, "import time; time.sleep(0.3)")
            jobs = JobQueue()
            first_job = jobs.submit(first, cwd=tmp)
            deadline = time.time() + 3
            while not jobs.busy and time.time() < deadline:
                time.sleep(.01)
            with self.assertRaisesRegex(RuntimeError, 'Outra geração TTS'):
                jobs.submit_exclusive([sys.executable, '-c', 'pass'], cwd=tmp)
            self.assertEqual(first_job.wait(5).returncode, 0)
            exclusive = jobs.submit_exclusive([sys.executable, '-c', 'pass'], cwd=tmp)
            self.assertEqual(exclusive.wait(5).returncode, 0)
            jobs.shutdown()


if __name__ == '__main__':
    unittest.main()
