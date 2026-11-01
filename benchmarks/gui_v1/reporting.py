import csv
import html
import json
from collections import Counter
from pathlib import Path

from .common import CRITERIA, metric_sum, read, write

def collect(root):
    root = Path(root)
    manifest = read(root / 'manifest.json')
    rows = []
    for model in manifest['models']:
        for case in manifest['cases']:
            for rep in range(1, manifest['repetitions'] + 1):
                p = root / model / 'runs' / f'{case}-r{rep}'
                s = read(p / 'summary.json') if (p / 'summary.json').exists() else {}
                a = read(p / 'assessment.json') if (p / 'assessment.json').exists() else {}
                peak = 0
                if (p / 'metrics.jsonl').exists():
                    for line in (p / 'metrics.jsonl').read_text().splitlines():
                        try: peak = max(peak, metric_sum(json.loads(line).get('metrics', {}), 'vllm:kv_cache_usage_perc'))
                        except (ValueError, TypeError): pass
                rows.append({'model': model, 'case': case, 'rep': rep, 'run': f'{case}-r{rep}',
                             'result': a.get('result', s.get('status', 'NOT_STARTED')),
                             'wall_s': s.get('wall_s'), 'gui_actions': s.get('gui_action_count'),
                             'decisions': s.get('decision_count'), 'kv_peak': peak,
                             'notes': a.get('notes', ''), 'evidence': str(p.relative_to(root))})
    return manifest, rows

def build(root):
    root = Path(root)
    manifest, rows = collect(root)
    lines = ['# GUI v1 自动benchmark', '',
             '每轮由脚本独立准备页面，正式动作由Qwen + 原生Venus执行；本脚本不调用Codex。', '',
             '聊天记录保留，复核必须检查本轮新增消息。模板/分辨率固定，当前首图与实时网页可能变化。与历史清理消息复测的条件不同。', '',
             'REVIEW是待独立判分，PENDING_RECEIVER是发送侧通过但待收件核验；两者不自动算PASS或FAIL。环境无效/中断也单列。', '',
             '## 配置', '', '冻结配置见manifest.json；实际参数见每轮api-NNN-request.json。两模型默认temperature=1，可通过配置文件或--temperature修改。', '',
             '```json', json.dumps(manifest['config']['sampling'], ensure_ascii=False, indent=2), '```', '',
             '模型专属presence_penalty：'+json.dumps({m:manifest['config']['models'][m]['presence_penalty'] for m in manifest['models']}), '',
             '## 结果', '']
    groups = {}
    for model in manifest['models']:
        group = [x for x in rows if x['model'] == model]
        counts = Counter(x['result'] for x in group)
        planned = len(group)
        groupstats = {}
        for prefix in ('A', 'E'):
            rs = [x for x in group if x['case'].startswith(prefix)]
            c = Counter(x['result'] for x in rs)
            groupstats[prefix] = dict(c)
        groups[model] = {'counts': dict(counts), 'groups': groupstats, 'planned': planned}
        unresolved = sum(v for k,v in counts.items() if k not in ('PASS','FAIL','ENVIRONMENT_INVALID'))
        lines += [f"### {model}", '', f"计划{planned}轮；结果：{dict(counts)}。", '',
                  f"已确认PASS/计划轮次：{counts['PASS']}/{planned}；待定项全部核验后才报告最终成功率。" if unresolved else
                  f"有效轮次成功率：{counts['PASS']}/{counts['PASS']+counts['FAIL']}（环境无效另列）。", '',
                  '原子任务：'+str(groupstats['A'])+'；端到端任务：'+str(groupstats['E'])+'。', '',
                  '| 用例 | '+' | '.join(f'第{i}次' for i in range(1,manifest['repetitions']+1))+' | 耗时秒（各次） | GUI动作数（各次） | 失败/待核验原因（对应轮次） |',
                  '|'+ '|'.join(['---']*(manifest['repetitions']+4))+'|']
        for case in manifest['cases']:
            rs = [x for x in group if x['case'] == case]
            reasons = '；'.join(f"第{x['rep']}次：{x['notes']}" for x in rs if x['result'] not in ('PASS','NOT_STARTED')) or '—'
            reasons = reasons.replace('|', '｜').replace('\n', ' ')
            lines.append('| '+case+' | '+' | '.join(x['result'] for x in rs)+' | '+', '.join('—' if x['wall_s'] is None else f"{x['wall_s']:.2f}" for x in rs)+' | '+', '.join(str(x['gui_actions']) if x['gui_actions'] is not None else '—' for x in rs)+' | '+reasons+' |')
    lines += ['', '## 证据与复核', '', '运行目录包含framework.log、API请求响应、逐步截图、初末截图、录像、metrics.jsonl。GUI计数排除无法解析的None和完成声明；双击计2。', '',
              'KV Cache启动配置与Prefix状态见模型目录的server-info.json；每轮metrics.jsonl保存运行指标。非零KV占用证明该窗口使用缓存，零采样峰值不等于关闭缓存。', '',
              '录屏采用720×1600，模型截图保持1080×2400。静态尾帧可能省略，录像时长不等于任务墙钟时间。', '',
              '用`./run_gui_benchmark.sh review 结果目录`打开本地批量复核页。完成接收端检查后勾选确认，再保存PASS。']
    (root / 'REPORT.md').write_text('\n'.join(lines)+'\n')
    write(root / 'analysis.json', {'groups': groups, 'rows': rows})
    with (root / 'ledger.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['model']);w.writeheader();w.writerows(rows)
    checklist=[dict(x,criterion=CRITERIA[x['case']]) for x in rows if x['result'] in ('REVIEW','PENDING_RECEIVER','INTERRUPTED')]
    write(root / 'review-queue.json', checklist)
    write(root / 'status.json', {'groups': groups, 'review_count':len(checklist)})
    return rows
