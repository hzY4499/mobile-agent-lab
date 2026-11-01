"""Local-only, token-protected human review; no external API or model calls."""
import html
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from .common import CRITERIA, read
from .oracle import record_review
from .reporting import build, collect

STYLE='''body{font:16px system-ui;margin:25px auto;max-width:1250px;background:#f4f5f7;color:#172033}a{color:#1469c2}section{background:white;padding:20px;margin:20px 0;border-radius:12px}.shots{display:flex;gap:15px}img{width:30%;height:auto;object-fit:contain;align-self:start}video{width:320px;max-height:650px}textarea{width:95%;height:70px}button{padding:10px 25px;margin:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere}.pill{background:#e9efff;padding:4px 10px;border-radius:12px}'''

def serve(root,port):
    root=Path(root).resolve();token=secrets.token_urlsafe(32);lock=threading.Lock()
    def url(route,**kwargs):return route+'?'+urlencode({'token':token,**kwargs})
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,code,body,mime='text/html; charset=utf-8'):
            data=body.encode() if isinstance(body,str) else body
            self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
        def authorized(self):
            q=parse_qs(urlparse(self.path).query)
            return q,q.get('token',[''])[0]==token
        def do_GET(self):
            q,ok=self.authorized()
            if not ok:return self.respond(403,'需要本地复核链接中的访问令牌。')
            route=urlparse(self.path).path
            if route=='/file':
                target=(root/q.get('path',[''])[0]).resolve()
                if not target.is_relative_to(root) or not target.is_file():return self.respond(404,'文件不存在')
                return self.respond(200,target.read_bytes(),mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            with lock:
                _,rows=collect(root)
            choices=[r for r in rows if (root/r['evidence']/'assessment.json').exists()]
            selected=q.get('run',[''])[0]
            row=next((r for r in choices if r['evidence']==selected),None)
            page=['<!doctype html><meta charset="utf-8"><title>GUI benchmark复核</title><style>'+STYLE+'</style>',
                  '<h1>GUI benchmark集中复核</h1><p>核验本轮新操作；模型说完成不等于通过。图片/视频/链接必须核验接收端。</p>',
                  '<p><a href="'+html.escape(url('/file',path='REPORT.md'))+'">报告</a> · <a href="'+html.escape(url('/file',path='ledger.csv'))+'">CSV</a></p>']
            if row:
                p=root/row['evidence'];s=read(p/'summary.json');a=read(p/'assessment.json')
                page+=['<section><h2>'+html.escape(row['model']+'/'+row['run'])+' <span class="pill">'+a['result']+'</span></h2>',
                       '<p>'+html.escape(CRITERIA[row['case']])+'</p><p>'+html.escape(a['notes'])+'</p><div class="shots">']
                for name in ['initial.png','final.png']:
                    if (p/name).exists():page.append('<img alt="'+name+'" src="'+html.escape(url('/file',path=str((p/name).relative_to(root))))+'">')
                page.append('</div>')
                for v in sorted(p.glob('screen-*.mp4')):
                    page.append('<video controls preload="metadata" src="'+html.escape(url('/file',path=str(v.relative_to(root))))+'"></video>')
                page+=['<details><summary>每步模型响应、执行和截图</summary>']
                for f in sorted(p.glob('api-*-response.json')):
                    d=read(f);index=f.name.split('-')[1]
                    call=next((c for c in s.get('calls',[]) if c['index']==int(index)),{})
                    step=call.get('step',int(index));shot=p/f'observation-{step:03}.png'
                    page+=['<h3>请求 '+index+'</h3><pre>'+html.escape(json.dumps(d['choices'][0]['message'],ensure_ascii=False,indent=2))+'</pre>']
                    if shot.exists():page+=['<a href="'+html.escape(url('/file',path=str(shot.relative_to(root))))+'">查看该步截图</a>']
                page+=['<pre>'+html.escape(json.dumps(s.get('executions',[]),ensure_ascii=False,indent=2))+'</pre></details>',
                       '<p><a href="'+html.escape(url('/file',path=str((p/'framework.log').relative_to(root))))+'">framework.log</a></p>',
                       '<form method="post" action="'+html.escape(url('/save'))+'"><input type="hidden" name="run" value="'+html.escape(row['evidence'])+'">',
                       '<textarea name="note" required placeholder="依据：对应新消息/页面/媒体/失败原因">'+html.escape(a['notes'])+'</textarea>',
                       '<p><label><input type="checkbox" name="receiver" value="yes">我已核验本轮收件内容、次数和顺序；图片可打开、视频完整播放/时长或链接正文符合相应用例要求</label></p>',
                       '<button name="verdict" value="PASS">发送侧/任务通过</button><button name="verdict" value="FAIL">失败</button><button name="verdict" value="ENVIRONMENT_INVALID">环境无效</button></form>',
                       '<p>端到端未勾选接收确认时保存为PENDING_RECEIVER。环境无效只能用于准备条件或设备故障，不能用于抹去模型错误。</p></section>']
            page+=['<section><h2>全部轮次</h2>']
            for r in choices:
                page.append('<p><a href="'+html.escape(url('/',run=r['evidence']))+'">'+html.escape(r['model']+'/'+r['run'])+'</a> — '+html.escape(r['result'])+'</p>')
            page+=['</section>'];self.respond(200,'\n'.join(page))
        def do_POST(self):
            _,ok=self.authorized()
            if not ok:return self.respond(403,'拒绝访问')
            if urlparse(self.path).path!='/save':return self.respond(404,'不存在')
            length=int(self.headers.get('Content-Length','0'))
            if length>32000:return self.respond(413,'内容过长')
            data=parse_qs(self.rfile.read(length).decode())
            try:
                with lock:
                    _,rows=collect(root);valid={r['evidence'] for r in rows}
                    run=data['run'][0]
                    if run not in valid:raise ValueError('非法轮次')
                    record_review(root/run,data['verdict'][0],data['note'][0],data.get('receiver')==['yes'])
                    build(root)
                self.send_response(303);self.send_header('Location',url('/',run=run));self.end_headers()
            except (KeyError,ValueError,FileNotFoundError) as e:self.respond(400,html.escape(str(e)))
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'浏览器打开：http://127.0.0.1:{server.server_port}/?token={token}',flush=True)
    print('仅监听本机；Ctrl+C关闭。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
