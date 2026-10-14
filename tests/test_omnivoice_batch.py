import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from studio.tts import omnivoice_batch as ob


class FakeJob:
    def __init__(self, result):
        self.result = result
        self.cancelled = False

    def wait(self, timeout=None):
        return self.result

    def cancel(self):
        self.cancelled = True


def ok():
    return SimpleNamespace(returncode=0, cancelled=False, timed_out=False, logs=[], error=None)


class PlanTests(unittest.TestCase):
    def test_skips_empty_nonverbal_and_unverified_lines_with_reason(self):
        def build(line_run, h, text):
            if h == 3:
                raise ValueError('OmniVoice requer um vínculo GXT→PAK/banco/slot original verificado.')
            return ['python', 'x', str(line_run)]
        texts = {1: 'Olá, tudo bem?', 2: '~z~', 3: 'Sem vínculo', 4: 'Outra fala'}
        items, skipped = ob.plan_lines(build, 'TRU2', [1, 2, 3, 4], texts, {1: 'A', 2: 'B', 3: 'C', 4: 'D'}, Path('run'))
        self.assertEqual([i.key for i in items], ['A', 'D'])
        self.assertEqual([k for k, _ in skipped], ['B', 'C'])
        self.assertIn('vínculo', skipped[1][1])

    def test_each_line_gets_its_own_output_folder(self):
        items, _ = ob.plan_lines(lambda lr, h, t: ['x'], 'T', [1, 255], {1: 'a b', 255: 'c d'}, {}, Path('run'))
        self.assertNotEqual(items[0].output_dir, items[1].output_dir)
        self.assertTrue(str(items[1].output_dir).endswith('000000FF/output') or
                        str(items[1].output_dir).replace('\\', '/').endswith('000000FF/output'))

    def test_limit_counts_generable_lines_and_stop_interrupts(self):
        texts = {i: 'fala %d' % i for i in range(1, 6)}
        items, _ = ob.plan_lines(lambda lr, h, t: ['x'], 'T', list(texts), texts, {}, Path('r'), limit=2)
        self.assertEqual(len(items), 2)
        items, _ = ob.plan_lines(lambda lr, h, t: ['x'], 'T', list(texts), texts, {}, Path('r'), stop=lambda: True)
        self.assertEqual(items, [])


class RunTests(unittest.TestCase):
    def items(self, n):
        return [ob.BatchItem(i, 'K%d' % i, 'fala %d' % i, ['x'], Path('run/lines/%08X' % i)) for i in range(1, n + 1)]

    def test_runs_in_series_imports_each_and_survives_one_failure(self):
        calls, logs = [], []
        results = {1: ok(), 2: SimpleNamespace(returncode=1, cancelled=False, timed_out=False, error=None,
                                                logs=['Traceback', 'ValueError: janela desatualizada']), 3: ok()}
        order = iter([1, 2, 3])

        def submit(command, cwd, on_line, timeout):
            calls.append(command)
            return FakeJob(results[next(order)])
        imported = []

        def import_line(item):
            imported.append(item.h)
            return {'valid': 1, 'quarantined': 0, 'imported': [{'line_id': 'T|%08x' % item.h}]}
        ctl = ob.BatchController()
        summary = ob.run_batch(self.items(3), [('S', 'sem fala')], 'T', submit, import_line, ctl, logs.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(imported, [1, 3])
        self.assertEqual((summary.valid, len(summary.failed), len(summary.skipped)), (2, 1, 1))
        self.assertIn('janela desatualizada', summary.failed[0][1])
        self.assertTrue(ctl.done)
        self.assertIn('Total de falas a processar: 3', logs)
        self.assertIn('[2/3] K2', logs)

    def test_cancel_stops_before_next_line_and_keeps_imported(self):
        ctl = ob.BatchController()
        jobs = []

        def submit(command, cwd, on_line, timeout):
            job = FakeJob(ok())
            jobs.append(job)
            return job

        def import_line(item):
            ctl.cancel()  # usuário cancela depois da primeira fala
            return {'valid': 1, 'quarantined': 0, 'imported': [{'line_id': 'x'}]}
        summary = ob.run_batch(self.items(3), [], 'T', submit, import_line, ctl)
        self.assertTrue(summary.cancelled)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(summary.valid, 1)

    def test_quarantined_and_empty_imports_are_reported_not_counted_as_valid(self):
        seq = iter([{'valid': 0, 'quarantined': 1, 'imported': []}, {'valid': 0, 'quarantined': 0, 'imported': []}])
        summary = ob.run_batch(self.items(2), [], 'T', lambda *a: FakeJob(ok()), lambda item: next(seq),
                               ob.BatchController())
        self.assertEqual((summary.valid, summary.quarantined, len(summary.failed)), (0, 1, 1))

    def test_timeout_is_a_failure_of_that_line_only(self):
        timed = SimpleNamespace(returncode=None, cancelled=False, timed_out=True, logs=[], error=None)
        results = iter([timed, ok()])
        summary = ob.run_batch(self.items(2), [], 'T', lambda *a: FakeJob(next(results)),
                               lambda item: {'valid': 1, 'imported': []}, ob.BatchController())
        self.assertTrue(summary.timed_out)
        self.assertEqual((summary.valid, len(summary.failed)), (1, 1))


class EnvironmentErrorTests(unittest.TestCase):
    GPU = SimpleNamespace(returncode=1, cancelled=False, timed_out=False, error=None, logs=[
        'RuntimeError: GPU fora do limite seguro da canary (mínimo 5120 MiB livres e até 10% de uso).'])

    def items(self, n):
        return [ob.BatchItem(i, 'K%d' % i, 'fala %d' % i, ['x'], Path('run/lines/%08X' % i)) for i in range(1, n + 1)]

    def test_busy_gpu_is_retried_with_a_wait_and_then_succeeds(self):
        results = iter([self.GPU, self.GPU, ok()])
        waits, logs = [], []
        summary = ob.run_batch(self.items(1), [], 'T', lambda *a: FakeJob(next(results)),
                               lambda item: {'valid': 1, 'imported': []}, ob.BatchController(), logs.append,
                               wait=waits.append)
        self.assertEqual(len(waits), 2)
        self.assertEqual((summary.valid, summary.failed), (1, []))
        self.assertTrue(any('Nova tentativa' in l for l in logs))

    def test_same_error_on_consecutive_lines_aborts_instead_of_burning_the_table(self):
        calls = []

        def submit(*a):
            calls.append(1)
            return FakeJob(self.GPU)
        summary = ob.run_batch(self.items(50), [], 'T', submit, lambda item: {}, ob.BatchController(),
                               wait=lambda s: None)
        self.assertIn('GPU fora do limite', summary.aborted)
        self.assertEqual(len(summary.failed), ob.SAME_ERROR_ABORT)
        self.assertEqual(len(calls), ob.SAME_ERROR_ABORT * (ob.TRANSIENT_RETRIES + 1))

    def test_a_success_between_failures_resets_the_streak(self):
        other = SimpleNamespace(returncode=1, cancelled=False, timed_out=False, error=None, logs=['ValueError: x'])
        results = iter([other, other, ok(), other, other, ok()])
        summary = ob.run_batch(self.items(6), [], 'T', lambda *a: FakeJob(next(results)),
                               lambda item: {'valid': 1, 'imported': []}, ob.BatchController(),
                               wait=lambda s: None)
        self.assertEqual(summary.aborted, '')
        self.assertEqual((summary.valid, len(summary.failed)), (2, 4))

    def test_cancel_during_retry_wait_stops_the_batch(self):
        ctl = ob.BatchController()
        summary = ob.run_batch(self.items(3), [], 'T', lambda *a: FakeJob(self.GPU), lambda item: {}, ctl,
                               wait=lambda s: ctl.cancel())
        self.assertTrue(summary.cancelled)


if __name__ == '__main__':
    unittest.main()
