# -*- coding: utf-8 -*-
"""Serial, cancellable subprocess execution for heavyweight TTS jobs."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from studio.core.utils import CREATE_FLAGS, matar_arvore


@dataclass
class ProcessResult:
    returncode: Optional[int]
    cancelled: bool
    timed_out: bool
    logs: list[str]
    error: Optional[str] = None


class ProcessJob:
    """Handle to one queued or running process."""

    def __init__(self, command: Sequence[str], cwd, on_line, timeout, max_log_lines):
        self.command = [os.fspath(part) for part in command]
        self.cwd = Path(cwd)
        self.on_line = on_line
        self.timeout = timeout
        self._logs = deque(maxlen=max_log_lines)
        self._lock = threading.RLock()
        self._cancel_requested = threading.Event()
        self._done = threading.Event()
        self._proc = None
        self._result = None

    @property
    def logs(self):
        with self._lock:
            return list(self._logs)

    @property
    def done(self):
        return self._done.is_set()

    def cancel(self):
        """Cancel a queued job or terminate its active process tree."""
        self._cancel_requested.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            matar_arvore(proc)

    def wait(self, timeout=None) -> ProcessResult:
        if not self._done.wait(timeout):
            raise TimeoutError('TTS process job is still running.')
        with self._lock:
            return self._result

    def _log(self, line):
        line = str(line).rstrip('\r\n')
        # A hostile or broken child must not retain an unbounded single line.
        if len(line) > 16_384:
            line = line[:16_384] + ' …[truncated]'
        with self._lock:
            self._logs.append(line)
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:
                # A UI/log consumer must not take down process supervision.
                pass

    def _finish(self, result):
        with self._lock:
            self._result = ProcessResult(
                returncode=result.returncode,
                cancelled=result.cancelled,
                timed_out=result.timed_out,
                logs=list(self._logs),
                error=result.error,
            )
        self._done.set()


class JobQueue:
    """Run one child at a time, without coupling process work to a UI toolkit."""

    def __init__(self, max_log_lines=500):
        self.max_log_lines = max(1, int(max_log_lines))
        self._pending = queue.Queue()
        self._lock = threading.RLock()
        self._active = None
        self._jobs = set()
        self._shutdown = False
        self._keep_model_loaded = False
        from studio.tts.resident import ResidentWorker
        self._resident = ResidentWorker()
        self._worker = threading.Thread(target=self._run, name='tts-job-queue', daemon=True)
        self._worker.start()

    @property
    def keep_model_loaded(self):
        with self._lock:
            return self._keep_model_loaded

    def set_keep_model_loaded(self, enabled):
        with self._lock:
            self._keep_model_loaded = bool(enabled) and not self._shutdown
            if not self._keep_model_loaded and self._active is None:
                self._resident.close()

    @property
    def active_count(self):
        with self._lock:
            return int(self._active is not None)

    @property
    def busy(self):
        with self._lock:
            return bool(self._jobs)

    def submit(self, command, cwd, on_line=None, timeout=600) -> ProcessJob:
        if not command:
            raise ValueError('command must contain an executable')
        job = ProcessJob(command, cwd, on_line, timeout, self.max_log_lines)
        with self._lock:
            if self._shutdown:
                raise RuntimeError('JobQueue is shut down.')
            self._jobs.add(job)
            self._pending.put(job)
        return job

    def submit_exclusive(self, command, cwd, on_line=None, timeout=600) -> ProcessJob:
        """Submit only if no other queued or active TTS job owns the shared queue."""
        if not command:
            raise ValueError('command must contain an executable')
        with self._lock:
            if self._shutdown:
                raise RuntimeError('JobQueue is shut down.')
            if self._jobs:
                raise RuntimeError('Outra geração TTS está ativa ou na fila; aguarde ou cancele antes de tentar novamente.')
            job = ProcessJob(command, cwd, on_line, timeout, self.max_log_lines)
            self._jobs.add(job)
            self._pending.put(job)
        return job

    def shutdown(self, wait=False):
        """Cancel active and queued work; optionally wait for the supervisor."""
        with self._lock:
            if self._shutdown:
                worker = self._worker
            else:
                self._shutdown = True
                worker = self._worker
                for job in tuple(self._jobs):
                    job.cancel()
                self._pending.put(None)
                self._resident.close()
        if wait and threading.current_thread() is not worker:
            worker.join()

    def _run(self):
        while True:
            job = self._pending.get()
            if job is None:
                return
            if job._cancel_requested.is_set():
                job._finish(ProcessResult(None, True, False, []))
                with self._lock:
                    self._jobs.discard(job)
                continue
            with self._lock:
                if self._shutdown:
                    job._cancel_requested.set()
                else:
                    self._active = job
            if job._cancel_requested.is_set():
                job._finish(ProcessResult(None, True, False, []))
                with self._lock:
                    if self._active is job:
                        self._active = None
                    self._jobs.discard(job)
                continue
            try:
                result = self._execute(job)
            except Exception as exc:
                result = ProcessResult(None, job._cancel_requested.is_set(), False, [], str(exc))
            with self._lock:
                if self._active is job:
                    self._active = None
                self._jobs.discard(job)
                if not self._keep_model_loaded or self._shutdown:
                    self._resident.close()
            job._finish(result)

    def _execute(self, job):
        is_omni = '--request' in job.command and any(
            Path(part).name.lower() == 'omnivoice_tts.py' for part in job.command)
        with self._lock:
            retained = self._keep_model_loaded and not self._shutdown
            if not (retained and is_omni):
                self._resident.close()
        if retained and is_omni:
            return self._resident.execute(job)
        try:
            child_env = os.environ.copy()
            child_env['PYTHONIOENCODING'] = 'utf-8'
            child_env['PYTHONUTF8'] = '1'
            proc = subprocess.Popen(
                job.command,
                cwd=str(job.cwd),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                creationflags=CREATE_FLAGS,
            )
        except Exception as exc:
            return ProcessResult(None, False, False, [], str(exc))

        with job._lock:
            job._proc = proc
        if job._cancel_requested.is_set():
            matar_arvore(proc)

        reader = threading.Thread(
            target=self._read_output, args=(job, proc), name='tts-job-output', daemon=True
        )
        reader.start()
        started = time.monotonic()
        timed_out = False
        while proc.poll() is None:
            if job._cancel_requested.is_set():
                matar_arvore(proc)
                break
            if job.timeout is not None and time.monotonic() - started >= float(job.timeout):
                timed_out = True
                matar_arvore(proc)
                break
            time.sleep(0.025)
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = proc.wait()
        reader.join(timeout=5)
        with job._lock:
            job._proc = None
        return ProcessResult(
            returncode=returncode,
            cancelled=job._cancel_requested.is_set() and not timed_out,
            timed_out=timed_out,
            logs=[],
        )

    @staticmethod
    def _read_output(job, proc):
        try:
            for line in proc.stdout:
                job._log(line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
