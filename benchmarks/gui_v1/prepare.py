"""Deterministic, verified preparation for the calibrated Honor phone.

These operations finish before the evaluated policy is created or timed.
No coordinates or reference images are supplied to the model.
"""
import io
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .common import ASSETS, EnvironmentIssue, now, write

# Only stable controls are compared; message bodies and gallery thumbnails vary.
REGIONS = {
 'main': [('main', (0, 2120, 1080, 2280), 10)],
 'search': [('search', (75, 100, 930, 215), 13)],
 'results': [('results', (80, 105, 930, 215), 13),
             ('results', (70, 375, 760, 600), 14)],
 'chat': [('chat', (330, 95, 760, 215), 10)],
 'empty_chat': [('chat', (330, 95, 760, 215), 10),
                ('chat', (160, 2160, 880, 2250), 12)],
 'draft': [('draft', (330, 95, 760, 215), 10),
           ('draft', (165, 2075, 880, 2205), 13)],
 'picker': [('picker', (320, 650, 750, 740), 13),
            ('picker', (0, 2140, 1080, 2260), 13)],
 'selected_photo': [('selected_photo', (320, 650, 750, 740), 13),
                    ('selected_photo', (0, 2140, 1080, 2260), 13)],
 'videos': [('videos', (320, 650, 750, 740), 13),
            ('videos', (0, 2140, 1080, 2260), 13)],
 'selected_video': [('selected_video', (320, 650, 750, 740), 13),
                    ('selected_video', (0, 2140, 1080, 2260), 13)],
}

def template_scores(image, state):
    if image.size != (1080, 2400):
        return [{'pass': False, 'reason': 'wrong image dimensions'}]
    values = []
    for reference, box, threshold in REGIONS[state]:
        ref = Image.open(ASSETS / (reference + '.png')).convert('RGB').crop(box)
        score = sum(ImageStat.Stat(ImageChops.difference(image.convert('RGB').crop(box), ref)).mean) / 3
        values.append({'reference': reference, 'box': box, 'mae': score,
                       'threshold': threshold, 'pass': score < threshold})
    return values

class Phone:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, timeout=25):
        try:
            p = subprocess.run(['adb', '-s', self.serial, *map(str, args)],
                               capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise EnvironmentIssue('ADB调用失败：' + type(e).__name__) from e
        if p.returncode:
            raise EnvironmentIssue('ADB连接/执行失败：' + p.stderr.decode(errors='replace')[:300])
        return p.stdout

    def image(self, path=None):
        data = self.adb('exec-out', 'screencap', '-p')
        try:
            im = Image.open(io.BytesIO(data)).convert('RGB')
            im.load()
        except Exception as e:
            raise EnvironmentIssue('截图无效或锁屏禁止截图') from e
        if max(ImageStat.Stat(im).mean) < 2:
            raise EnvironmentIssue('截图全黑，请检查锁屏/亮屏状态。')
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        return im

    def ready(self):
        if self.adb('get-state').strip() != b'device':
            raise EnvironmentIssue('手机未授权或离线')
        policy = self.adb('shell', 'dumpsys', 'window', 'policy').decode(errors='replace')
        if re.search(r'(?:mIsShowing|isStatusBarKeyguard|showingAndNotOccluded)=true', policy):
            raise EnvironmentIssue('手机已锁屏，请手动解锁后断点续跑。')
        self.adb('shell', 'svc', 'power', 'stayon', 'true')
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_WAKEUP')
        if self.image().size != (1080, 2400):
            raise EnvironmentIssue('分辨率与1080×2400准备模板不符。')
        keyboard = self.adb('shell', 'settings', 'get', 'secure', 'default_input_method').decode()
        if 'adbkeyboard' not in keyboard.lower():
            # Setup action, never part of the scored GUI loop.
            self.adb('shell', 'ime', 'set', 'com.android.adbkeyboard/.AdbIME')

    def tap(self, x, y):
        self.adb('shell', 'input', 'tap', x, y)
        time.sleep(.7)

    def back(self):
        self.adb('shell', 'input', 'keyevent', 4)
        time.sleep(.6)

    def text(self, value):
        self.adb('shell', shlex.join(['am', 'broadcast', '-a', 'ADB_CLEAR_TEXT']))
        if value:
            self.adb('shell', shlex.join(['am', 'broadcast', '-a', 'ADB_INPUT_TEXT', '--es', 'msg', value]))
        time.sleep(.7)

    def keyboard_shown(self):
        state = self.adb('shell', 'dumpsys', 'input_method').decode(errors='replace')
        match = re.search(r'\bmInputShown=(true|false)\b', state)
        if not match:
            raise EnvironmentIssue('无法核验键盘状态，停止准备以免误退聊天页。')
        return match.group(1) == 'true'

    def tree(self, path=None):
        self.adb('shell', 'uiautomator', 'dump', '/data/local/tmp/venus-benchmark-ui.xml', timeout=15)
        data = self.adb('shell', 'cat', '/data/local/tmp/venus-benchmark-ui.xml')
        if path:
            Path(path).write_bytes(data)
        return ET.fromstring(data)

    def foreground(self):
        text = self.adb('shell', 'dumpsys', 'window', 'windows').decode(errors='replace')
        return '\n'.join(line.strip() for line in text.splitlines() if 'mCurrentFocus' in line or 'mFocusedApp' in line)

    def media(self):
        videos = self.adb('shell', 'content', 'query', '--uri', 'content://media/external/video/media',
                          '--projection', '_id:_display_name:duration').decode(errors='replace')
        images = self.adb('shell', 'content', 'query', '--uri', 'content://media/external/images/media',
                          '--projection', '_id:_display_name:date_added').decode(errors='replace')
        durations = re.findall(r'duration=(\d+)', videos)
        if len(durations) != 1 or not 20000 <= int(durations[0]) <= 30000:
            raise EnvironmentIssue('需要唯一20–30秒测试视频；程序不会自行删除原媒体。')
        if 'Row:' not in images:
            raise EnvironmentIssue('图库没有可查询图片。')
        return {'videos': videos, 'images': images, 'note': '使用当前相册首图；未强制缩减为3张图片。'}

class Preparation:
    def __init__(self, phone, out):
        self.phone = phone
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.checks = []

    def require(self, state):
        image = self.phone.image(self.out / (state + '.png'))
        values = template_scores(image, state)
        self.checks.append({'state': state, 'at': now(), 'scores': values})
        write(self.out / 'checks.json', self.checks)
        if not all(v['pass'] for v in values):
            raise EnvironmentIssue('准备页面不符合模板：' + state + '；请查看preparation截图。')

    def main(self):
        p = self.phone
        p.adb('shell', 'am', 'force-stop', 'com.tencent.mm')
        p.adb('shell', 'am', 'start', '-n', 'com.tencent.mm/.ui.LauncherUI')
        time.sleep(5)
        for _ in range(8):
            if all(x['pass'] for x in template_scores(p.image(), 'main')):
                self.require('main')
                return
            p.back()
            p.adb('shell', 'am', 'start', '-n', 'com.tencent.mm/.ui.LauncherUI')
            time.sleep(1)
        self.require('main')

    def home(self):
        p = self.phone
        p.adb('shell', 'input', 'keyevent', 3)
        time.sleep(.8)
        # Scan both directions: the launcher does not wrap at the resource-library page.
        for index in range(18):
            if any((n.get('text') == '微信' or n.get('content-desc', '').startswith('微信'))
                   and n.get('package') == 'com.hihonor.android.launcher'
                   for n in p.tree().iter('node')):
                return
            x1,x2=(950,150) if index < 8 else (150,950)
            p.adb('shell', 'input', 'swipe', x1, 1700, x2, 1700, 450)
            time.sleep(.7)
        raise EnvironmentIssue('无法找到微信图标所在的桌面页。')

    def search(self):
        self.main()
        self.phone.tap(905, 150)
        time.sleep(1.5)
        self.require('search')

    def results(self):
        self.search()
        self.phone.tap(400, 178)
        self.phone.text('Y_Yao')
        time.sleep(2)
        self.require('results')

    def chat(self):
        self.results()
        self.phone.tap(320, 440)
        time.sleep(2)
        self.require('chat')
        self.clear_chat_draft()
        self.require('empty_chat')
        self.phone.image(self.out / 'chat-before.png')

    def clear_chat_draft(self):
        p = self.phone
        # A reopened chat may retain an active editor/keyboard (including an
        # invisible newline draft). In that layout y=2215 is the keyboard strip,
        # not the text field. Keep the focused editor instead of tapping it away.
        visible = p.keyboard_shown()
        self.checks.append({'state': 'keyboard-before-clear', 'at': now(), 'shown': visible})
        write(self.out / 'checks.json', self.checks)
        if not visible:
            p.tap(400, 2215)
            for _ in range(10):
                if p.keyboard_shown():
                    break
                time.sleep(.2)
            else:
                raise EnvironmentIssue('聊天输入框未获得焦点，未清空草稿或按返回。')
        p.text('')
        if p.keyboard_shown():
            p.back()
        if p.keyboard_shown():
            raise EnvironmentIssue('键盘未成功收起，保留聊天页并停止准备。')

    def picker(self):
        self.chat()
        self.phone.tap(1015, 2215)
        self.phone.tap(140, 1850)
        time.sleep(1.2)
        self.require('picker')

    def videos(self):
        self.picker()
        self.phone.tap(905, 680)
        self.phone.tap(380, 980)
        time.sleep(1.2)
        self.require('videos')

    def browser_home(self):
        self.phone.adb('shell', shlex.join(['am', 'start', '-a', 'android.intent.action.VIEW',
                       '-d', 'https://www.baidu.com', '-p', 'com.honor.browser']))
        time.sleep(5)

    def browser_empty(self):
        self.browser_home()
        fields=[]
        for node in self.phone.tree().iter('node'):
            if 'EditText' in node.get('class','') and node.get('resource-id')=='index-kw':
                box=list(map(int,re.findall(r'\d+',node.get('bounds',''))))
                if len(box)==4 and box[2]>box[0] and box[3]>box[1]:fields.append(box)
        if not fields:
            raise EnvironmentIssue('百度首页搜索输入框不可见。')
        x1,y1,x2,y2=fields[0]
        self.phone.tap((x1+x2)//2,(y1+y2)//2)
        self.phone.text('')
        nodes = list(self.phone.tree().iter('node'))
        fields = [x for x in nodes if 'EditText' in x.get('class', '')
                  and x.get('resource-id') in ('index-kw','kw')]
        if not fields or any(x.get('text') for x in fields):
            raise EnvironmentIssue('浏览器输入框未成功清空。')

    def browser_results(self):
        self.browser_empty()
        self.phone.text('天竺葵')
        self.phone.adb('shell', 'input', 'keyevent', 66)
        time.sleep(7)
        for _ in range(2):
            self.phone.adb('shell', 'input', 'swipe', 550, 550, 550, 1950, 500)
            time.sleep(1)
        for _ in range(16):
            for node in self.phone.tree().iter('node'):
                if node.get('text') == '天竺葵 - 百度百科':
                    box = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
                    if len(box) == 4 and box[2] > box[0] and 270 < box[1] < 2040:
                        return box
            self.phone.adb('shell', 'input', 'swipe', 550, 1650, 550, 1050, 1000)
            time.sleep(1.5)
        raise EnvironmentIssue('实时搜索结果未找到预检的第一条普通网页。')

    def run(self, case):
        p = self.phone
        p.ready()
        if case == 'A01':
            self.main(); self.home()
        elif case.startswith('E'):
            self.chat(); self.main(); self.browser_home(); self.home()
        elif case == 'A02': self.main()
        elif case == 'A03': self.search()
        elif case == 'A04': self.results()
        elif case in ('A05', 'A06', 'A07'):
            self.chat()
            if case == 'A06':
                p.tap(400, 2215); p.text('照片拍好了'); self.require('draft')
        elif case in ('A08', 'A09', 'A10'):
            self.picker()
            if case in ('A09', 'A10'):
                p.tap(215, 820); self.require('selected_photo')
        elif case in ('A11', 'A12'):
            self.videos()
            if case == 'A12':
                p.tap(215, 820); self.require('selected_video')
        elif case == 'A13': self.browser_empty()
        elif case == 'A14': self.browser_results()
        elif case == 'A15':
            x1, y1, x2, y2 = self.browser_results()
            p.tap((x1+x2)//2, (y1+y2)//2); time.sleep(5)
            content = ET.tostring(p.tree(), encoding='unicode')
            if '天竺葵' not in content or 'baike' not in content.lower() and '百度百科' not in content:
                raise EnvironmentIssue('预检网页正文未打开。')
        else: raise ValueError(case)
        p.image(self.out / 'ready.png')
        p.tree(self.out / 'ready.xml')
        write(self.out / 'ready.json', {'case': case, 'at': now(), 'valid': True,
              'foreground': p.foreground(), 'chat_history': 'preserve'})
