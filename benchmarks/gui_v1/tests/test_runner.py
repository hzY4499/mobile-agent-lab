import copy
import tempfile
import unittest
from pathlib import Path

from benchmarks.gui_v1.common import EnvironmentIssue, exclusive_lock, gui_count, read, write
from benchmarks.gui_v1.cli import classify_existing
from benchmarks.gui_v1.oracle import assess, record_review
from benchmarks.gui_v1.service import load_config, request_parameters

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def summary(self,**kwargs):
        s={'case':'E01','rep':1,'status':'COMPLETED','native_result':[True,'success',None],
           'gui_action_count':0,'decisions':[{'action':{'action_type':'SUCCESS'}}]}
        s.update(kwargs);write(self.root/'summary.json',s);return s
    def test_native_success_never_proves_new_delivery(self):
        self.summary()
        self.assertEqual(assess(self.root)['result'],'REVIEW')
    def test_failure_and_environment_are_not_mixed(self):
        self.summary(status='TIMEOUT')
        self.assertEqual(assess(self.root)['result'],'FAIL')
        self.summary(status='ENVIRONMENT_ERROR',error='USB disconnected')
        self.assertEqual(assess(self.root)['result'],'INTERRUPTED')
    def test_malformed_actions_count_zero_and_fail(self):
        self.summary(decisions=[{'action':{'action_type':None}}]*5)
        self.assertEqual(assess(self.root)['result'],'FAIL')
        self.assertEqual(gui_count([{'action':{'action_type':None}},{'action':{'action_type':'DoubleCLK'}}]),2)
    def test_receiver_required_and_review_audited(self):
        self.summary(gui_action_count=5)
        write(self.root/'assessment.json',assess(self.root))
        self.assertEqual(record_review(self.root,'PASS','实际新发送1次')['result'],'PENDING_RECEIVER')
        self.assertEqual(record_review(self.root,'PASS','收件文字准确且1次',True)['result'],'PASS')
        self.assertEqual(len(read(self.root/'review-history.json')),2)
    def test_interrupted_not_silently_rerun_or_passed(self):
        self.summary(status='RUNNING')
        self.assertEqual(classify_existing(self.root),'BLOCKED')
        self.assertEqual(read(self.root/'summary.json')['status'],'INTERRUPTED')
        with self.assertRaises(ValueError):record_review(self.root,'PASS','可能收到',True)
        record_review(self.root,'ENVIRONMENT_INVALID','USB断开；已检查消息情况')
        self.assertEqual(classify_existing(self.root),'INVALID')
    def test_completed_failure_is_retained_on_resume(self):
        self.summary(status='TIMEOUT');write(self.root/'assessment.json',assess(self.root))
        self.assertEqual(classify_existing(self.root),'DONE')
    def test_api_failure_not_passable_as_completed(self):
        self.summary(native_result=[False,'policy_failed',None],calls=[{'error':'APIConnectionError'}])
        write(self.root/'assessment.json',assess(self.root))
        self.assertEqual(classify_existing(self.root),'BLOCKED')
        with self.assertRaises(ValueError):record_review(self.root,'PASS','模型未完成',True)
    def test_step_limit_not_overridden_by_review(self):
        self.summary(native_result=[False,'max_steps',None])
        write(self.root/'assessment.json',assess(self.root))
        with self.assertRaises(ValueError):record_review(self.root,'PASS','步数已耗尽',True)
    def test_concurrent_runner_is_rejected(self):
        with exclusive_lock(str(self.root)):
            with self.assertRaises(EnvironmentIssue):
                with exclusive_lock(str(self.root)):pass
    def test_temperature_override_frozen_for_both_native_penalty_differs(self):
        original=load_config()
        c=load_config(temperature=.7)
        p=request_parameters(c,'qwen35');q=request_parameters(c,'qwen38')
        self.assertEqual(p['temperature'],.7);self.assertEqual(q['temperature'],.7)
        self.assertEqual(p['presence_penalty'],original['models']['qwen35']['presence_penalty'])
        self.assertEqual(q['presence_penalty'],original['models']['qwen38']['presence_penalty'])
        for key in ['top_p','seed','max_tokens','extra_body']:self.assertEqual(p[key],q[key])
        # The user is allowed to edit config.yaml; zero must not be treated as
        # an absent override, and overrides must never mutate their source file.
        self.assertEqual(load_config(temperature=0)['sampling']['temperature'],0)
        self.assertEqual(load_config(),original)
    def test_config_validation_rejects_unsupported_target(self):
        import yaml
        c=load_config();c['recipient']='someone else';p=self.root/'c.yaml';p.write_text(yaml.safe_dump(c))
        with self.assertRaises(ValueError):load_config(p)

if __name__=='__main__':unittest.main()
