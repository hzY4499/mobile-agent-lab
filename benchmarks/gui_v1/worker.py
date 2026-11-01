"""Instrument the unmodified native Venus run loop; setup/oracles are external."""
import argparse, base64, copy, csv, gzip, hashlib, json, logging, os, pickle
from pathlib import Path
import signal, subprocess, sys, threading, time
from datetime import datetime
import requests, yaml
from PIL import Image, ImageStat

from .common import FW, TASKS, EnvironmentIssue, Interrupted, gui_count, now, write
from .prepare import Phone
from .service import Service, request_parameters
sys.path.insert(0, str(FW))
ROOT = None
SERIAL = None
CONFIG = None
MODEL = None

def dump(path, data):
    write(path, data)

def adb(*args, timeout=30):
    return subprocess.run(['adb','-s',SERIAL,*args], capture_output=True, check=True, timeout=timeout).stdout

def screenshot(path):
    Path(path).write_bytes(adb('exec-out','screencap','-p'))

def hierarchy(path):
    try:
        adb('shell','uiautomator','dump','/data/local/tmp/venus-bench.xml',timeout=12)
        Path(path).write_bytes(adb('shell','cat','/data/local/tmp/venus-bench.xml'))
    except Exception as e:
        Path(str(path)+'.error').write_text(type(e).__name__)

def redact(v):
    if isinstance(v,dict): return {k:redact(x) for k,x in v.items()}
    if isinstance(v,list): return [redact(x) for x in v]
    if isinstance(v,str) and v.startswith('data:image/'):
        head,data=v.split(',',1)
        return {'mime':head,'image_sha256':hashlib.sha256(base64.b64decode(data)).hexdigest()}
    return v

def metrics(text):
    prefixes=('vllm:kv_cache_usage_perc','vllm:prefix_cache_queries_total','vllm:prefix_cache_hits_total',
              'vllm:mm_cache_queries_total','vllm:mm_cache_hits_total','vllm:num_preemptions_total',
              'vllm:num_requests_running','vllm:num_requests_waiting','vllm:request_success_total')
    out={}
    for line in text.splitlines():
        if line.startswith(prefixes):
            k,v=line.rsplit(' ',1); out[k]=float(v)
    return out

class Deadline(BaseException): pass
class RuntimeEnvironment(BaseException): pass

def run(case, rep):
    from app.run_handler import RunHandler
    power=adb('shell','dumpsys','window','policy').decode(errors='replace')
    if 'mIsShowing=true' in power:
        raise RuntimeError('Phone is locked; user must unlock before any formal attempt')
    out=ROOT/'runs'/f'{case}-r{rep}'
    out.mkdir(parents=True,exist_ok=False)
    os.chdir(FW)
    config = copy.deepcopy(CONFIG)
    mapping=yaml.safe_load((FW/'config/app_mapping.yaml').read_text())['app_mapping']
    service=Service(config)
    key=service.key
    os.environ['API_KEY']=key
    logging.basicConfig(filename=str(out/'framework.log'), level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s', force=True)
    budget=config['end_to_end' if case.startswith('E') else 'atomic']
    limit=budget['seconds']
    expected=request_parameters(config, MODEL)
    params=dict(expected['extra_body'], top_p=expected['top_p'],
                presence_penalty=expected['presence_penalty'], seed=expected['seed'])
    handler=RunHandler(device_id=SERIAL,trace_dir=str(out/'traces'),policy_type='ui_venus_2',
                       ep_config={'step_limit':budget['steps'],'mini_app_name':'Android App'},
                       app_mapping=mapping,model_url=config['service']['url'],
                       model_name=expected['model'],temperature=expected['temperature'],
                       n_img=config['n_img'],generation_params=params)
    summary={'case':case,'rep':rep,'purpose':TASKS[case], 'config':config,
             'model':expected['model'],'expected_request_parameters':expected,'wall_limit_s':limit,
             'started_at':now(),'status':'RUNNING','oracle':'PENDING_REVIEW',
             'receiver':'PENDING' if case.startswith('E') else 'NOT_REQUIRED',
             'calls':[],'http_attempts':[],'decisions':[],'executions':[],'adb_attempts':[]}
    def save(): dump(out/'summary.json',summary)
    save()
    screenshot(out/'initial.png'); hierarchy(out/'initial.xml')
    with Image.open(out/'initial.png') as im:
        if max(ImageStat.Stat(im.convert('RGB')).mean)<2:
            summary['status']='ENVIRONMENT_INVALID'; summary['oracle']='ENVIRONMENT_INVALID'
            summary['notes']='Black initial screenshot; no task instruction issued.'; save(); return
    (out/'foreground-initial.txt').write_bytes(adb('shell','dumpsys','window','windows'))
    stop=threading.Event(); recording_stop=threading.Event(); ready=threading.Event()
    base=config['service']['url'].removesuffix('/v1')
    def sample():
        session=requests.Session(); session.headers['Authorization']='Bearer '+key
        with (out/'metrics.jsonl').open('w') as f:
            while not stop.is_set():
                try:
                    r=session.get(base+'/metrics',timeout=4); r.raise_for_status()
                    row={'at':time.time(),'metrics':metrics(r.text)}
                except Exception as e: row={'at':time.time(),'error':type(e).__name__}
                f.write(json.dumps(row)+'\n'); f.flush(); stop.wait(1)
    recording=[]
    recording_error=[]
    def video():
        for i in range(1, int(limit / 180) + 3):
            remote=f'/data/local/tmp/venus-auto-{os.getpid()}-{case}-r{rep}-{i}.mp4'
            proc=subprocess.Popen(['adb','-s',SERIAL,'shell',f'echo $$; exec screenrecord --size 720x1600 --bit-rate 2000000 --time-limit 180 {remote}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            pid=proc.stdout.readline().decode().strip()
            started=time.time()
            # Check recorder startup before starting the task clock.
            time.sleep(.6)
            startup_alive = proc.poll() is None
            if not startup_alive:
                recording_error.append("screenrecord exited at startup")
            ready.set()
            while proc.poll() is None:
                if recording_stop.wait(.25):
                    if pid.isdigit():
                        try: adb('shell','kill','-2',pid)
                        except Exception: pass
                    break
            try: _, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired: proc.terminate(); proc.communicate(timeout=5)
            else:
                (out/f'screen-{i:02}.stderr').write_bytes(stderr)
            local=out/f'screen-{i:02}.mp4'
            try:
                adb('pull',remote,str(local),timeout=40); adb('shell','rm','-f',remote)
                recording.append({'file':local.name,'started':started,'stopped':time.time(),'bytes':local.stat().st_size,
                                  'startup_pid':pid,'startup_process_alive':startup_alive,'resolution':'720x1600'})
            except Exception as e: recording.append({'error':type(e).__name__})
            if recording_stop.is_set(): break
    monitor=threading.Thread(target=sample,daemon=True); monitor.start()
    recorder=threading.Thread(target=video,daemon=True); recorder.start(); ready.wait(8)
    create=handler.policy.client.chat.completions.create
    def traced_create(**kwargs):
        # Only max_tokens is not exposed by the original policy constructor.
        kwargs['max_tokens']=expected['max_tokens']
        actual={k:v for k,v in kwargs.items() if k!='messages'}
        if actual != expected:
            raise RuntimeEnvironment('实际请求参数与冻结配置不符。')
        n=len(summary['calls'])+1
        rec={'index':n,'step':handler.runtime_context.step,'started':time.time()}
        summary['calls'].append(rec)
        dump(out/f'api-{n:03}-request.json',redact(kwargs)); save()
        try:
            response=create(**kwargs)
            raw=response.model_dump(mode='json')
            if raw.get('model')!=expected['model']:
                raise RuntimeEnvironment('响应模型与冻结配置不符。')
            dump(out/f'api-{n:03}-response.json',raw)
            rec.update(elapsed_s=time.time()-rec['started'],usage=raw.get('usage'),finish_reason=raw['choices'][0]['finish_reason'],model=raw.get('model'))
            return response
        except BaseException as e:
            rec.update(elapsed_s=time.time()-rec['started'],error=type(e).__name__); raise
        finally: save()
    handler.policy.client.chat.completions.create=traced_create
    # Capture the serialized HTTP body, including SDK-internal retries. extra_body
    # is merged here, so these files show the actual top-level server parameters.
    wire_expected=copy.deepcopy(expected)
    wire_expected.update(wire_expected.pop('extra_body'))
    def wire_request(request):
        payload=json.loads(request.content)
        if {k:v for k,v in payload.items() if k!='messages'} != wire_expected:
            raise RuntimeEnvironment('序列化后的HTTP参数与冻结配置不符。')
        n=len(summary['http_attempts'])+1
        request.extensions['benchmark_attempt']=n
        summary['http_attempts'].append({'index':n,'api_call':len(summary['calls']),
                                        'step':handler.runtime_context.step,'started':time.time()})
        dump(out/f'http-{n:03}-request.json',redact(payload));save()
    def wire_response(response):
        n=response.request.extensions['benchmark_attempt']
        rec=summary['http_attempts'][n-1]
        rec.update(status_code=response.status_code,elapsed_s=time.time()-rec['started'])
        response.read()
        try:body=response.json()
        except ValueError:body={'text':response.text[:4000].replace(key,'[REDACTED]')}
        dump(out/f'http-{n:03}-response.json',body);save()
    handler.policy.client._client.event_hooks['request'].append(wire_request)
    handler.policy.client._client.event_hooks['response'].append(wire_response)
    handle=handler.handle_client_req
    def traced_handle(data):
        step=handler.runtime_context.step
        if not data.get('screenshot_str'):
            raise EnvironmentIssue('手机截图中断')
        (out/f'observation-{step:03}.png').write_bytes(base64.b64decode(data['screenshot_str']))
        result=handle(data)
        summary['decisions'].append({'step':step,'at':time.time(),'action':result[1],'conclusion':result[5]})
        save(); print(json.dumps({'run':out.name,'step':step,'action':result[1]},ensure_ascii=False),flush=True)
        return result
    handler.handle_client_req=traced_handle
    request_screen=handler.req_from_client
    def guarded_screen():
        data=request_screen()
        if not data.get('screenshot_str'):
            raise EnvironmentIssue('ADB截图失败；本轮不自动重试。')
        return data
    handler.req_from_client=guarded_screen
    execute=handler.rsp_to_client
    active={'value':False}
    original_run=subprocess.run
    def traced_subprocess(*args,**kwargs):
        command=args[0] if args else kwargs.get('args',[])
        rec=None
        if active['value'] and threading.current_thread() is threading.main_thread() and isinstance(command,list) and command and Path(command[0]).name=='adb':
            rec={'step':handler.runtime_context.step,'at':time.time(),'argv':command}
            summary['adb_attempts'].append(rec)
        try:
            result=original_run(*args,**kwargs)
            if rec is not None: rec['returncode']=result.returncode
            return result
        except BaseException as e:
            if rec is not None: rec['error']=type(e).__name__
            raise
    subprocess.run=traced_subprocess
    def traced_execute(action):
        try:
            if adb('get-state').strip()!=b'device':
                raise EnvironmentIssue('手机断线')
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
            raise RuntimeEnvironment('ADB连接失败；正式轮次暂停，不重发。') from e
        rec={'step':handler.runtime_context.step,'action':copy.deepcopy(action),'started':time.time()}
        summary['executions'].append(rec); active['value']=True
        try:
            rec['success']=execute(action)
            if not rec['success']:
                # Action-format failures are model failures; connection failures are infrastructure.
                try: adb('get-state',timeout=5)
                except Exception as e: raise RuntimeEnvironment('动作执行时ADB连接丢失。') from e
            return rec['success']
        finally:
            active['value']=False; rec['elapsed_s']=time.time()-rec['started']; save()
    handler.rsp_to_client=traced_execute
    def alarm(signum, frame): raise Deadline()
    old=signal.signal(signal.SIGALRM,alarm)
    def interrupt(signum, frame): raise Interrupted('用户中止；保留现场，不自动重跑')
    old_int=signal.signal(signal.SIGINT,interrupt)
    old_term=signal.signal(signal.SIGTERM,interrupt)
    started=time.monotonic(); signal.setitimer(signal.ITIMER_REAL,limit)
    try:
        if recording_error or not ready.is_set():
            raise EnvironmentIssue('录屏启动失败；本轮未下发任务。')
        summary['instruction_issued_at']=now()
        summary['native_result']=handler.run(TASKS[case]); summary['status']='COMPLETED'
    except Deadline: summary['status']='TIMEOUT'
    except Interrupted as e:
        summary['status']='INTERRUPTED'; summary['error']=str(e)
    except (EnvironmentIssue,RuntimeEnvironment) as e:
        summary['status']='ENVIRONMENT_ERROR' if summary.get('instruction_issued_at') else 'ENVIRONMENT_INVALID'
        summary['error']=str(e)
    except BaseException as e:
        summary['status']='ERROR'; summary['error']=str(e).replace(key,'[REDACTED]'); summary['error_type']=type(e).__name__
    finally:
        signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,old)
        summary['wall_s']=time.monotonic()-started
        subprocess.run=original_run
        try: screenshot(out/'final.png')
        except Exception as e: summary['screenshot_error']=type(e).__name__
        recording_stop.set(); stop.set(); recorder.join(timeout=50); monitor.join(timeout=5)
        hierarchy(out/'final.xml')
        summary['video_segments']=recording
        summary['decision_count']=len(summary['decisions'])
        summary['gui_action_count']=gui_count(summary['executions'])
        summary['adb_attempt_count']=len(summary['adb_attempts'])
        summary['finished_at']=now(); save()
        signal.signal(signal.SIGINT,old_int);signal.signal(signal.SIGTERM,old_term)
        print(json.dumps({k:summary.get(k) for k in ('case','rep','status','native_result','wall_s','decision_count','gui_action_count')},ensure_ascii=False),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path);parser.add_argument('model');parser.add_argument('case',choices=TASKS);parser.add_argument('rep',type=int)
    args=parser.parse_args()
    manifest=json.loads((args.root/'manifest.json').read_text())
    CONFIG=manifest['config'];MODEL=args.model;SERIAL=CONFIG['device']
    ROOT=args.root/args.model
    try:
        run(args.case,args.rep)
    except BaseException as e:
        # The parent distinguishes preparation/no-instruction failures from completed attempts.
        dump(ROOT/'worker-error.json',{'type':type(e).__name__,'at':now(),'case':args.case,'rep':args.rep})
        path=ROOT/'runs'/f'{args.case}-r{args.rep}'/'summary.json'
        if not path.exists():
            dump(path,{'case':args.case,'rep':args.rep,'status':'ENVIRONMENT_INVALID',
                      'error':'工作进程在下发任务前启动失败：'+type(e).__name__,
                      'calls':[],'decisions':[],'executions':[],'adb_attempts':[],
                      'wall_s':0,'decision_count':0,'gui_action_count':0,'finished_at':now()})
        raise SystemExit(2)
