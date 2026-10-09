import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import agents, runner
from app.server import App
from app.store import Store

class AdapterRoutingTests(unittest.TestCase):
    def test_pi_and_shelley_keep_separate_adapters(self):
        with patch('app.native.run_agent',return_value={'status':'completed'}) as native:
            agents.run_agent('agent','work','out','prompt','model',variant={})
            native.assert_called_once()
        with patch('app.pi.run_agent',return_value={'status':'completed'}) as pi:
            agents.run_agent('agent','work','out','prompt','model',variant={'agent':'pi','options':{'provider':'example'}})
            self.assertEqual(pi.call_args.kwargs['options'],{'provider':'example'})

    def test_pi_preparation_pins_binary_without_building_shelley(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'pi';path.write_text('fake executable');path.chmod(0o755)
            with patch('app.agents.shutil.which',return_value=str(path)),patch('app.agents.subprocess.run') as cli:
                cli.return_value.returncode=0;cli.return_value.stdout='1.0.0\n'
                version=agents.pi_version({'options':{'provider':'example'}})
            app=App.__new__(App)
            with patch('app.runner._build') as build:
                prepared=app.build(version,{},Path(tmp)/'build',None)
            build.assert_not_called();self.assertEqual(prepared['agent'],'pi')
            path.write_text('changed')
            with self.assertRaisesRegex(ValueError,'changed'):
                agents.prepare_pi(version,Path(tmp)/'new-build')

    def test_reuse_clones_completed_evidence_and_rejects_changed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App.__new__(App);app.store=Store(Path(tmp)/'bench.sqlite3')
            task={'id':'task','prompt':'same task','base_commit':'abc','test_patch':'same test'}
            source={'id':'source','benchmark_id':'old','version':'After','model':'model','task_id':'task','attempt':2,'status':'complete','passed':True,'events':[{'id':'event'}]}
            app.store.put('runs',source)
            app.store.put('benchmarks',{'id':'old','reasoning':'medium','agent_note':'same note','frozen_tasks':[task]})
            bench={'id':'new','models':['model'],'task_ids':['task'],'attempts':1,'reasoning':'medium','agent_note':'same note','frozen_tasks':[dict(task)],'versions':[{'label':'Baseline','reuse_run_id':'source'}]}
            actual=app.reused_runs(bench)
            self.assertEqual(len(actual),1);self.assertEqual(actual[0]['_source_run_id'],'source');self.assertEqual(actual[0]['recorded_attempt'],2)
            self.assertEqual(app.store.require('runs','source')['attempt'],2)
            bench['frozen_tasks'][0]['test_patch']='different test'
            with self.assertRaisesRegex(ValueError,'frozen task'):
                app.reused_runs(bench)

    def test_creation_reuses_one_version_and_queues_only_missing_pi_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App.__new__(App);app.store=Store(Path(tmp)/'bench.sqlite3');app.models=[{'id':'model'}]
            task={'id':'task','title':'Task','status':'ready','prompt':'same','base_commit':'abc','test_patch':'test'}
            app.store.put('tasks',task)
            app.store.put('benchmarks',{'id':'old','reasoning':'medium','agent_note':'','frozen_tasks':[task]})
            app.store.put('runs',{'id':'source','benchmark_id':'old','version':'After','model':'model','task_id':'task','attempt':2,'status':'complete','passed':True})
            resolved=[{'label':'Shelley','reuse_run_id':'source'},{'label':'Pi','agent':'pi','sha':'hash'}]
            class Jobs:
                def submit(self,*args,**kwargs):
                    return {'id':'job'}
            app.jobs=Jobs()
            with patch.object(app,'resolve_versions',return_value=resolved):
                bench=app.create_benchmark({'task_ids':['task'],'versions':[{}],'models':['model'],'reasoning':'medium','attempts':1})
            runs=app.store.list('runs',benchmark_id=bench['id'])
            self.assertEqual([(r['version'],r['status']) for r in runs],[('Shelley','complete'),('Pi','queued')])
            self.assertEqual(app.store.require('runs','source')['attempt'],2)
