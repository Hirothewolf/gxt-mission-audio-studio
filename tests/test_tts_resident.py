import json, os, sys, tempfile, unittest
from pathlib import Path
from studio.tts.jobs import JobQueue

WORKER = '''import json, os, sys, time
if '--serve' in sys.argv:
 for line in sys.stdin:
  item=json.loads(line); req=json.load(open(item['request_path']))
  time.sleep(req.get('sleep',0))
  print(json.dumps({'pid':os.getpid(),'speed':req.get('speed')}),flush=True)
  print('__OMNI_RESULT__'+json.dumps({'id':item['id'],'returncode':0}),flush=True)
else:
 print(json.dumps({'pid':os.getpid()}),flush=True)
'''
class ResidentTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
  self.script=self.root/'omnivoice_tts.py'; self.script.write_text(WORKER)
  self.q=JobQueue()
 def tearDown(self):
  self.q.shutdown(wait=True); self.temp.cleanup()
 def run_take(self, name, **params):
  request=self.root/(name+'.json'); request.write_text(json.dumps(params))
  return self.q.submit([sys.executable,'-u',str(self.script),'--request',str(request)],self.root,timeout=3)
 def test_optional_reuse_and_release(self):
  self.assertFalse(self.q.keep_model_loaded)
  self.q.set_keep_model_loaded(True)
  a=self.run_take('a',speed=.9).wait(5); b=self.run_take('b',speed=1.2).wait(5)
  x=json.loads(a.logs[0]); y=json.loads(b.logs[0])
  self.assertEqual(x['pid'],y['pid']); self.assertEqual(y['speed'],1.2)
  self.q.set_keep_model_loaded(False)
  c=self.run_take('c').wait(5)
  self.assertNotEqual(json.loads(c.logs[0])['pid'],x['pid'])
 def test_shutdown_releases_idle_process(self):
  self.q.set_keep_model_loaded(True)
  self.assertEqual(self.run_take('a').wait(5).returncode,0)
  proc=self.q._resident.proc
  self.assertIsNone(proc.poll())
  self.q.shutdown(wait=True)
  self.assertIsNotNone(proc.poll())
 def test_default_does_not_retain_process(self):
  a=self.run_take('a').wait(5); b=self.run_take('b').wait(5)
  self.assertNotEqual(json.loads(a.logs[0])['pid'],json.loads(b.logs[0])['pid'])
  self.assertIsNone(self.q._resident.proc)
 def test_timeout_restarts_worker(self):
  self.q.set_keep_model_loaded(True)
  request=self.root/'slow.json'; request.write_text('{"sleep":5}')
  job=self.q.submit([sys.executable,'-u',str(self.script),'--request',str(request)],self.root,timeout=.15)
  self.assertTrue(job.wait(5).timed_out)
  self.assertEqual(self.run_take('next').wait(5).returncode,0)
 def test_cancel_restarts_worker(self):
  self.q.set_keep_model_loaded(True)
  job=self.run_take('slow',sleep=5); job.cancel()
  self.assertTrue(job.wait(5).cancelled)
  self.assertEqual(self.run_take('next').wait(5).returncode,0)
if __name__=='__main__': unittest.main()
