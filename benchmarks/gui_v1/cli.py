import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import requests

from .common import (ASSETS, DEFAULT_OUTPUT, FW, REPO, EnvironmentIssue, Interrupted,
                     exclusive_lock, now, parse_cases, read, sha, write)
from .oracle import assess
from .prepare import Phone, Preparation
from .reporting import build
from .service import CONFIG, Service, load_config, request_parameters

STOP = False

def stop_signal(signum, frame):
    global STOP
    STOP = True

def fingerprints():
    files = list(Path(__file__).parent.glob('*.py')) + list(ASSETS.glob('*.png'))
    files += [FW / name for name in ['app/run_handler.py','policy/ui_venus_2_policy.py',
              'processor/ui_venus_2_processor.py','config/app_mapping.yaml']]
    return {str(p):sha(p) for p in files}

def new_job(args):
    config = load_config(args.config, args.temperature)
    cases = parse_cases(args.cases)
    repeats = config['repetitions'] if args.repeats is None else args.repeats
    if repeats < 1 or repeats > 100:
        raise ValueError('重复次数应为1–100。')
    models = ['qwen35','qwen38'] if args.model == 'both' else [args.model]
    suffix = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    root = (args.output or DEFAULT_OUTPUT / (suffix+'-'+args.model)).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    config['repetitions'] = repeats
    manifest = {'schema':1,'created_at':now(),'models':models,'cases':cases,'repetitions':repeats,
                'config':config,'fingerprints':fingerprints(),'switch_service':not args.no_switch,
                'chat_history':'preserve','state':'NEW'}
    write(root / 'manifest.json', manifest)
    (root / 'frozen-config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    for model in models:
        (root / model).mkdir()
    build(root)
    return root

def classify_existing(folder):
    """Never repeat a possibly delivered side effect without explicit review."""
    if not folder.exists():
        return 'NEW'
    if not (folder / 'summary.json').exists():
        return 'BLOCKED'
    s = read(folder / 'summary.json')
    if s['status'] == 'RUNNING':
        s.update(status='INTERRUPTED',error='上次进程未正常收尾；不能自动重发。',finished_at=now())
        write(folder / 'summary.json',s)
    if not (folder / 'assessment.json').exists():
        write(folder / 'assessment.json', assess(folder))
    a = read(folder / 'assessment.json')
    if a['result']=='ENVIRONMENT_INVALID':
        return 'INVALID'
    if (s['status'] in ('RUNNING','INTERRUPTED','ERROR','ENVIRONMENT_ERROR') or a['result']=='INTERRUPTED') and a['result'] != 'FAIL':
        return 'BLOCKED'
    return 'DONE'

def run_worker(root, model, case, rep, seconds):
    folder=root/model
    log=folder/'console.log'
    with log.open('a') as output:
        proc=subprocess.Popen([sys.executable,'-m','benchmarks.gui_v1.worker',str(root),model,case,str(rep)],
                              cwd=REPO,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+seconds+180
        last=0;sent_stop=False
        while proc.poll() is None:
            if STOP or (root/'STOP').exists() or time.monotonic()>deadline:
                if not sent_stop:
                    os.kill(proc.pid,signal.SIGTERM);sent_stop=True;deadline=time.monotonic()+75
                elif time.monotonic()>deadline:
                    os.killpg(proc.pid,signal.SIGKILL)
            if time.monotonic()-last>20:
                p=folder/'runs'/f'{case}-r{rep}'/'summary.json'
                s=read(p) if p.exists() else {}
                print(f"  {model}/{case}-r{rep}: 已记录{len(s.get('decisions',[]))}个决策",flush=True)
                last=time.monotonic()
            time.sleep(.5)
        return proc.returncode

def verify_artifacts(folder):
    videos=[]
    for p in sorted(folder.glob('screen-*.mp4')):
        proc=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=width,height',
                             '-of','json',str(p)],capture_output=True,text=True,timeout=15)
        videos.append({'file':p.name,'bytes':p.stat().st_size,'valid':proc.returncode==0,
                       'metadata':json.loads(proc.stdout) if proc.returncode==0 else proc.stderr})
    required=['summary.json','assessment.json','initial.png','final.png','framework.log','metrics.jsonl']
    missing=[x for x in required if not (folder/x).exists()]
    result={'missing':missing,'videos':videos,'readable_video':bool(videos) and all(x['valid'] for x in videos),
            'note':'可读不等于全部墙钟时长均有视频帧；缺失证据必须单独记录。'}
    write(folder/'evidence-check.json',result)
    return result

def execute(root, retry_invalid=False):
    manifest=read(root/'manifest.json');config=manifest['config']
    current=fingerprints()
    for name,value in manifest['fingerprints'].items():
        if current.get(name)!=value:
            raise EnvironmentIssue('代码或页面模板已变化；为保证复现请新建实验，不沿用旧目录。')
    with exclusive_lock(config['device']+'|'+config['service']['url']):
        if (root/'STOP').exists(): (root/'STOP').unlink()
        start_ticks=Path('/proc/self/stat').read_text().split()[21]
        write(root/'control.json',{'pid':os.getpid(),'start_ticks':start_ticks,'at':now()})
        phone=Phone(config['device'])
        try:
            phone.ready()
            versions={}
            for package in ['com.tencent.mm','com.honor.browser']:
                text=phone.adb('shell','dumpsys','package',package).decode(errors='replace')
                versions[package]=[x.strip() for x in text.splitlines() if 'versionName=' in x or 'versionCode=' in x]
            write(root/'device-preflight.json',{'at':now(),'serial':config['device'],
                  'resolution':config['resolution'],'versions':versions,'foreground':phone.foreground()})
            needed_media=any(x in manifest['cases'] for x in ['A07','A08','A09','A10','A11','A12','E02','E03','E04'])
            if needed_media:write(root/'media-manifest.json',phone.media())
            service=Service(config)
            for model in manifest['models']:
                slots=[]
                for case in manifest['cases']:
                    for rep in range(1,manifest['repetitions']+1):
                        p=root/model/'runs'/f'{case}-r{rep}'
                        state=classify_existing(p)
                        if state=='BLOCKED':
                            raise EnvironmentIssue(f'{model}/{p.name}上次中断，先在复核页确认是否发生发送。判环境无效后用resume --retry-invalid，或依据实际证据记FAIL后继续。')
                        if state=='INVALID':
                            if not retry_invalid:
                                raise EnvironmentIssue(f'{p.name}是环境无效；确认恢复后使用resume --retry-invalid。')
                            archive=root/model/'environment-invalid'/f'{p.name}-{time.time_ns()}'
                            archive.parent.mkdir(parents=True,exist_ok=True)
                            shutil.move(str(p),archive)
                            prep=root/model/'preparation'/p.name
                            if prep.exists():shutil.move(str(prep),archive/'preparation')
                        if state!='DONE':slots.append((case,rep))
                if not slots:continue
                print(f'准备服务 {model}；实际请求参数：{request_parameters(config,model)}',flush=True)
                service.ensure(model,root/model,manifest['switch_service'],lambda:STOP)
                service.warmup(model,root/model)
                for case,rep in slots:
                    if STOP or (root/'STOP').exists():raise Interrupted('已停止')
                    print(f'自动准备 {model}/{case}-r{rep} …',flush=True)
                    prep=root/model/'preparation'/f'{case}-r{rep}'
                    if prep.exists():
                        archive=root/model/'preparation-history'/f'{case}-r{rep}-{time.time_ns()}'
                        archive.parent.mkdir(exist_ok=True);shutil.move(str(prep),archive)
                    Preparation(phone,prep).run(case)
                    if STOP or (root/'STOP').exists():raise Interrupted('准备完成后停止，未下发本轮任务。')
                    phone.ready()
                    print(f'正式执行 {model}/{case}-r{rep}',flush=True)
                    seconds=config['end_to_end' if case.startswith('E') else 'atomic']['seconds']
                    code=run_worker(root,model,case,rep,seconds)
                    p=root/model/'runs'/f'{case}-r{rep}'
                    if not (p/'summary.json').exists():
                        raise EnvironmentIssue(f'{p.name}启动失败，详见console.log。')
                    if read(p/'summary.json')['status']=='RUNNING':
                        s=read(p/'summary.json');s.update(status='INTERRUPTED',error='进程异常退出');write(p/'summary.json',s)
                    a=assess(p);write(p/'assessment.json',a)
                    evidence=verify_artifacts(p);build(root)
                    print(f"记录完成：{case}-r{rep} → {a['result']}",flush=True)
                    if STOP:raise Interrupted('已保存当前记录并停止。')
                    if code or a['result'] in ('INTERRUPTED','ENVIRONMENT_INVALID'):
                        raise EnvironmentIssue('运行中断/环境异常，已保存现场并暂停；不会自动覆盖或重发。')
                    if evidence['missing'] or not evidence['readable_video']:
                        raise EnvironmentIssue('证据文件不完整，当前记录保留，暂停以免后续录像继续丢失。')
            write(root/'execution.json',{'state':'EXECUTION_COMPLETE','at':now(),'note':'执行结束；REVIEW与接收端待定项仍需独立核验。'})
        except (EnvironmentIssue,Interrupted,KeyboardInterrupt,requests.RequestException,subprocess.SubprocessError) as e:
            reason=str(e) if isinstance(e,(EnvironmentIssue,Interrupted)) else type(e).__name__+'：外部服务/命令失败，已保留现场。'
            write(root/'execution.json',{'state':'PAUSED','at':now(),'reason':reason})
            build(root)
            print('已暂停：'+reason,flush=True)
            return 2
        finally:
            if (root/'control.json').exists(): (root/'control.json').unlink()
        build(root)
        print(f'执行结束。报告：{root}/REPORT.md\n集中复核：./run_gui_benchmark.sh review "{root}"',flush=True)
        return 0

def main():
    p=argparse.ArgumentParser(description='固定程序运行GUI测试集v1；自动准备、记录、断点续跑与集中复核。')
    sub=p.add_subparsers(dest='command')
    run=sub.add_parser('run',help='新建实验并执行（默认20用例×3次）')
    run.add_argument('--model',choices=['qwen35','qwen38','both'],default='qwen38')
    run.add_argument('--config',type=Path,default=CONFIG)
    run.add_argument('--temperature',type=float,help='覆盖配置中的温度，作用于本次所选模型')
    run.add_argument('--cases',default='all');run.add_argument('--repeats',type=int)
    run.add_argument('--output',type=Path);run.add_argument('--no-switch',action='store_true')
    resume=sub.add_parser('resume',help='继续未执行轮次；不会覆盖已完成的FAIL')
    resume.add_argument('root',type=Path);resume.add_argument('--retry-invalid',action='store_true')
    for command in ['report','status','stop','review']:
        cmd=sub.add_parser(command);cmd.add_argument('root',type=Path)
        if command=='review':cmd.add_argument('--port',type=int,default=8765)
    prep=sub.add_parser('prepare-check',help='仅自动准备并检查页面，不调用模型、不发送消息')
    prep.add_argument('--cases',default='all');prep.add_argument('--config',type=Path,default=CONFIG)
    prep.add_argument('--output',type=Path)
    argv=sys.argv[1:]
    if argv and argv[0].startswith('--') and argv[0] not in ('--help','-h'):argv=['run',*argv]
    args=p.parse_args(argv)
    if not args.command:p.print_help();return 0
    signal.signal(signal.SIGINT,stop_signal);signal.signal(signal.SIGTERM,stop_signal)
    try:
        if args.command=='run':
            root=new_job(args);print('结果目录：'+str(root),flush=True);return execute(root)
        if args.command=='prepare-check':
            config=load_config(args.config);phone=Phone(config['device']);cases=parse_cases(args.cases)
            root=(args.output or DEFAULT_OUTPUT/('prepare-check-'+str(time.time_ns()))).resolve()
            with exclusive_lock(config['device']+'|'+config['service']['url']):
                phone.ready()
                for case in cases:
                    if STOP:raise Interrupted('准备检查已停止')
                    print('检查自动准备：'+case,flush=True);Preparation(phone,root/case).run(case)
            print('准备检查完成：'+str(root));return 0
        root=args.root.expanduser().resolve()
        if args.command=='resume':return execute(root,args.retry_invalid)
        if args.command=='report':build(root);print(root/'REPORT.md')
        elif args.command=='status':build(root);print(json.dumps(read(root/'status.json'),ensure_ascii=False,indent=2))
        elif args.command=='stop':
            if not (root/'manifest.json').exists():raise ValueError('不是benchmark结果目录')
            (root/'STOP').touch()
            if (root/'control.json').exists():
                control=read(root/'control.json');stat=Path(f"/proc/{control['pid']}/stat")
                if stat.exists() and stat.read_text().split()[21]==control['start_ticks']:os.kill(control['pid'],signal.SIGTERM)
            print('停止请求已写入，当前记录将保存。')
        elif args.command=='review':
            from .review_server import serve
            signal.signal(signal.SIGINT,signal.default_int_handler)
            serve(root,args.port)
    except (EnvironmentIssue,Interrupted,ValueError,FileNotFoundError) as e:
        print(str(e),file=sys.stderr);return 2
    return 0

if __name__=='__main__':raise SystemExit(main())
