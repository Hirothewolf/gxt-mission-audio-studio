"""Private stdio worker: retained weights, one request at a time, no network port."""
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from studio.core.utils import CREATE_FLAGS, matar_arvore

class ResidentWorker:
    def __init__(self):
        self.proc = None
        self.key = None
        self.lines = None

    def close(self):
        proc, self.proc = self.proc, None
        self.key = None
        if proc is not None:
            if proc.poll() is None:
                matar_arvore(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stdin:
                proc.stdin.close()

    def execute(self, job):
        from studio.tts.jobs import ProcessResult
        index = job.command.index('--request')
        command = job.command[:index] + ['--serve']
        key = (tuple(command), str(job.cwd))
        if self.proc is None or self.proc.poll() is not None or self.key != key:
            self.close()
            env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
            self.proc = subprocess.Popen(command, cwd=str(job.cwd), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=CREATE_FLAGS)
            self.key = key
            self.lines = queue.Queue()
            proc, lines = self.proc, self.lines
            def read():
                try:
                    for line in proc.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)
                    proc.stdout.close()
            threading.Thread(target=read, daemon=True, name='omnivoice-resident-output').start()
        proc, lines = self.proc, self.lines
        token = uuid.uuid4().hex
        with job._lock:
            job._proc = proc
        started = time.monotonic()
        try:
            if not job._cancel_requested.is_set():
                proc.stdin.write(json.dumps({'id': token, 'request_path': job.command[index+1]})+'\n')
                proc.stdin.flush()
            while True:
                timed_out = job.timeout is not None and time.monotonic()-started >= float(job.timeout)
                if job._cancel_requested.is_set() or timed_out:
                    self.close()
                    return ProcessResult(proc.poll(), job._cancel_requested.is_set() and not timed_out, timed_out, [])
                try:
                    line = lines.get(timeout=.05)
                except queue.Empty:
                    if proc.poll() is not None:
                        self.close()
                        return ProcessResult(proc.returncode, False, False, [], 'O processo de voz encerrou antes de concluir a fala.')
                    continue
                if line is None:
                    self.close()
                    return ProcessResult(proc.poll(), False, False, [], 'O processo de voz encerrou antes de concluir a fala.')
                if line.startswith('__OMNI_RESULT__'):
                    result = json.loads(line[len('__OMNI_RESULT__'):])
                    if result.get('id') != token:
                        raise RuntimeError('Resposta de voz não corresponde à geração atual.')
                    return ProcessResult(result['returncode'], False, False, [], result.get('error'))
                job._log(line)
        except Exception:
            self.close()
            raise
        finally:
            with job._lock:
                job._proc = None
