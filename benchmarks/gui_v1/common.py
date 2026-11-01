import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
FW = REPO / 'Venus_framework/Venus_framework_mobile'
ASSETS = Path(__file__).parent / 'assets'
DEFAULT_OUTPUT = Path('/home/kojoker/Desktop/HuaWei/测试结果/自动benchmark')
SERIAL = 'AVRUVB2519001424'
RECIPIENT = 'Y_Yao'
SEARCH = '天竺葵'
GUI_ACTIONS = {'CLK', 'DoubleCLK', 'SWIPE', 'INPUT', 'BACK', 'PressHome',
               'PressMenu', 'PressEnter', 'LongPress', 'REOPEN'}
TASKS = {
 'A01':'打开微信。', 'A02':'打开微信搜索。', 'A03':'在搜索框输入“Y_Yao”。',
 'A04':'打开与Y_Yao的聊天。', 'A05':'输入“照片拍好了”，先不要发送。',
 'A06':'发送输入框里的消息。', 'A07':'打开聊天里的相册。',
 'A08':'选中第一张照片，先不要发送。', 'A09':'取消选中这张照片。',
 'A10':'发送选中的照片。', 'A11':'选中这段视频，先不要发送。',
 'A12':'发送选中的视频。', 'A13':'搜索“天竺葵”。',
 'A14':'打开第一条非广告网页结果。', 'A15':'返回上一页。',
 'E01':'打开微信，给Y_Yao发送“今天下午三点一起喝茶”。',
 'E02':'把相册里的第一张照片发给Y_Yao。',
 'E03':'把相册里的那段视频发给Y_Yao。',
 'E04':'把相册里的第一张照片发给Y_Yao，然后发一句“这是今天拍的照片”。',
 'E05':'用浏览器搜索“天竺葵”，打开第一条非广告网页，把这个网页的链接发给Y_Yao。',
}
CRITERIA = {
 'A01':'微信主界面可见，不能仅停在恢复的私聊页。',
 'A02':'微信搜索页打开，输入框可用。',
 'A03':'搜索框准确包含Y_Yao。', 'A04':'进入Y_Yao私聊，顶部名称匹配。',
 'A05':'草稿准确为“照片拍好了”，没有新消息发送。',
 'A06':'本轮新增准确文字“照片拍好了”，无失败标记。',
 'A07':'进入发送媒体选择页，不能是聊天历史媒体页。',
 'A08':'仅首张图片勾选，未发送。', 'A09':'图片选中数量为零，未发送。',
 'A10':'本轮新增对应图片，无失败标记。',
 'A11':'仅目标视频勾选，预览不等于选中。',
 'A12':'本轮新增对应视频，结束时已上传完成且无失败标记。',
 'A13':'搜索结果页出现，搜索词准确为天竺葵。',
 'A14':'打开预检的第一条普通网页，正文可见。',
 'A15':'回到原搜索结果页，搜索词保留，没有多退一步。',
 'E01':'Y_Yao本轮收到一次准确茶邀请，无重复或误发。',
 'E02':'Y_Yao本轮收到一次指定首图，能打开，无其他素材。',
 'E03':'Y_Yao本轮收到唯一测试视频，完整播放且时长一致，无重复。',
 'E04':'Y_Yao本轮先收到首图再收到“这是今天拍的照片”，各一次且图片可打开。',
 'E05':'实际打开第一条非广告正文，将该页链接新发给Y_Yao；接收端能打开正文。',
}

class EnvironmentIssue(Exception):
    pass

class Interrupted(BaseException):
    pass

def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()

def read(path):
    return json.loads(Path(path).read_text())

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Status/report/review may write concurrently; never share a temporary name.
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name+'.',
                                     suffix='.tmp', delete=False) as f:
        temp = Path(f.name)
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def gui_count(executions):
    return sum(2 if x['action'].get('action_type') == 'DoubleCLK' else 1
               for x in executions if x['action'].get('action_type') in GUI_ACTIONS)

def parse_cases(spec):
    if spec == 'all':
        return list(TASKS)
    values = [x.strip().upper() for x in spec.split(',')]
    if not values or any(x not in TASKS for x in values) or len(set(values)) != len(values):
        raise ValueError('用例应为all或A01,A02,E01等，不允许重复。')
    return values

@contextlib.contextmanager
def exclusive_lock(name):
    path = Path('/tmp') / ('venus-gui-benchmark-' + hashlib.sha256(name.encode()).hexdigest()[:20] + '.lock')
    with path.open('a+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EnvironmentIssue('已有benchmark使用同一手机/服务，请先停止或等待。')
        yield

def parse_metrics(text):
    prefixes = ('vllm:kv_cache_usage_perc', 'vllm:prefix_cache_queries_total',
                'vllm:prefix_cache_hits_total', 'vllm:num_requests_running',
                'vllm:num_requests_waiting', 'vllm:num_preemptions_total',
                'vllm:request_success_total')
    return {line.rsplit(' ', 1)[0]: float(line.rsplit(' ', 1)[1])
            for line in text.splitlines() if line.startswith(prefixes)}

def metric_sum(values, name):
    return sum(v for k, v in values.items() if k == name or k.startswith(name + '{'))
