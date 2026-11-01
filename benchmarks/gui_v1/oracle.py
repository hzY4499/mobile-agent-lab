"""Conservative rules: never use the acting model's success claim as ground truth."""
from pathlib import Path

from PIL import Image

from .common import read, now, write
from .prepare import template_scores

def assess(folder):
    folder = Path(folder)
    s = read(folder / 'summary.json')
    status = s['status']
    result = {'result': 'REVIEW', 'sender_result': 'REVIEW',
              'receiver': 'PENDING' if s['case'].startswith('E') else 'NOT_REQUIRED',
              'notes': '需独立复核初末截图、实际动作和新消息；模型自报成功不计PASS。',
              'source': 'deterministic-rules-v1', 'at': now()}
    if status in ('INTERRUPTED', 'RUNNING', 'ENVIRONMENT_ERROR', 'ENVIRONMENT_INVALID', 'ERROR'):
        result.update(result='INTERRUPTED' if status != 'ENVIRONMENT_INVALID' else 'ENVIRONMENT_INVALID',
                      sender_result='UNKNOWN', notes=s.get('error', status) + '；保留记录，未计为模型失败。')
        return result
    native = s.get('native_result') or [False, 'unknown', None]
    reason = native[1]
    if reason == 'policy_failed' and any('error' in c for c in s.get('calls', [])):
        result.update(result='INTERRUPTED',sender_result='UNKNOWN',notes='推理API失败，保留调用错误，不计作模型任务失败。')
        return result
    if status == 'TIMEOUT' or reason in ('max_steps', 'call_user'):
        result.update(result='FAIL', sender_result='FAIL', receiver='NOT_REQUIRED_FOR_FAIL',
                      notes='超时/步数耗尽/请求人工协助：' + str(reason if status != 'TIMEOUT' else status))
        return result
    decisions = s.get('decisions', [])
    if decisions and not s.get('gui_action_count') and all(not (d.get('action') or {}).get('action_type') for d in decisions):
        result.update(result='FAIL', sender_result='FAIL', receiver='NOT_REQUIRED_FOR_FAIL',
                      notes='全部模型动作均无法解析，没有执行GUI动作。')
        return result
    if any((d.get('action') or {}).get('action_type') == 'CallUser' for d in decisions):
        result.update(result='FAIL', sender_result='FAIL', receiver='NOT_REQUIRED_FOR_FAIL', notes='模型请求人工协助。')
        return result
    # These five terminal-page rules were replay-checked against both historical
    # models. New send / media identity / order still need independent review.
    states={'A01':'main','A02':'search','A03':'results','A04':'chat','A07':'picker'}
    if s['case'] in states and (folder / 'final.png').exists() and s.get('gui_action_count', 0) > 0:
        scores = template_scores(Image.open(folder / 'final.png'), states[s['case']])
        if all(x['pass'] for x in scores):
            result.update(result='PASS', sender_result='PASS', notes='终态与已校准目标页面关键区域匹配：'+states[s['case']], rule_evidence=scores)
    return result

def record_review(folder, verdict, note, receiver=False):
    folder = Path(folder)
    s = read(folder / 'summary.json')
    existing = read(folder / 'assessment.json')
    if not note.strip():
        raise ValueError('请写明依据（新消息、页面、媒体或错误）。')
    if verdict not in ('PASS', 'FAIL', 'ENVIRONMENT_INVALID'):
        raise ValueError('不支持的判定')
    reason=(s.get('native_result') or [False, 'unknown'])[1]
    if (s['status']=='TIMEOUT' or reason in ('max_steps','call_user')) and verdict == 'PASS':
        raise ValueError('超时/步数耗尽/请求人工协助不满足通过条件，不能改为PASS。')
    if (s['status'] in ('RUNNING', 'INTERRUPTED', 'ENVIRONMENT_ERROR', 'ENVIRONMENT_INVALID', 'ERROR')
            or existing['result']=='INTERRUPTED') and verdict == 'PASS':
        raise ValueError('中断记录不能直接改为PASS；可归档为环境无效或依据证据记FAIL。')
    if verdict == 'PASS' and s['case'].startswith('E') and not receiver:
        verdict = 'PENDING_RECEIVER'
    value = {'result': verdict, 'sender_result': 'PASS' if verdict in ('PASS', 'PENDING_RECEIVER') else verdict,
             'receiver': ('PASS' if receiver else 'PENDING') if s['case'].startswith('E') and verdict in ('PASS', 'PENDING_RECEIVER') else 'NOT_REQUIRED',
             'notes': note.strip(), 'source': 'human-review', 'at': now()}
    audit = folder / 'review-history.json'
    history = read(audit) if audit.exists() else []
    history.append({'previous': existing, 'new': value})
    write(audit, history)
    write(folder / 'assessment.json', value)
    return value
