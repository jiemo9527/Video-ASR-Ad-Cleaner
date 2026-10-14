"""Optional real-browser integration against four local Scanner origins; Aria2 is a fixture."""
import os,sys,threading,tempfile,json
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit
import unittest
if not os.environ.get("SCANNER_TEST_OVERVIEW_HTML"):
    raise unittest.SkipTest("Set SCANNER_TEST_OVERVIEW_HTML to the static overview HTML")
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['SCANNER_DATABASE_URI']='sqlite:///'+str(Path(tempfile.gettempdir())/'panel-browser.db').replace('\\','/')
import app as scanner
from database import db, User, Task
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
PAGE=Path(os.environ['SCANNER_TEST_OVERVIEW_HTML'])
servers=[make_server('127.0.0.1',0,scanner.app,threaded=True) for _ in range(4)]
urls=['http://127.0.0.1:'+str(s.server_port) for s in servers]
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  txt=PAGE.read_text(encoding='utf-8')
  for old,new in zip(['https://nodenc.jiemo.de:5001','https://nodehd.jiemo.de:5001','https://scanner.jiemo.de','https://scanner2.jiemo.de'],urls):txt=txt.replace(old,new)
  self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(txt.encode())
 def log_message(self,*args):pass
panel=ThreadingHTTPServer(('127.0.0.1',0),Handler)
origin='http://localhost:'+str(panel.server_port)
os.environ['SCANNER_MONITOR_ORIGINS']=origin
with scanner.app.app_context():
 db.create_all();db.session.query(Task).delete();db.session.query(User).delete()
 db.session.add(User(id='browser-test',password_hash=generate_password_hash('test-only-password')))
 db.session.add(Task(filename='<img src=x onerror=alert(1)>.mkv',filepath='/missing/movie.mkv',status='processing',progress=61,overrides=json.dumps({'_audio_segments':{'01片头':{'status':'passed'},'02片尾':{'status':'failed'}}})))
 db.session.add(Task(filename='upload.mkv',filepath='/missing/upload.mkv',status='uploading',progress=45,upload_speed='5 MiB/s',upload_eta='10s'))
 db.session.commit()
def rpc(calls):
 if calls[0][0]=='aria2.getFiles':return [[{'path':'/downloads/movie.mkv','uris':[{'uri':'SECRET_DOWNLOAD'}]}]]
 return [{'downloadSpeed':'1048576','uploadSpeed':'0','numActive':'1','numWaiting':'2','numStopped':'0'},[{'gid':'a'*16,'status':'active','totalLength':'100','completedLength':'25','downloadSpeed':'1048576'}]]
scanner.aria2_multicall=rpc
for s in servers:threading.Thread(target=s.serve_forever,daemon=True).start()
threading.Thread(target=panel.serve_forever,daemon=True).start()
try:
 with sync_playwright() as pw:
  browser=pw.chromium.launch()
  ctx=browser.new_context(viewport={'width':1440,'height':1100})
  page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(origin+'/spd')
  assert page.locator('.node').count()==4,'Missing four-node overview'
  assert page.locator('.links a[target="_blank"]').count()==0, 'Open-node links must stay in the current tab'
  assert page.locator('.node.online').count()==0
  page.evaluate("window.postMessage({type:'scanner-monitor-grant',state:'fake',token:'fake',expiresAt:Date.now()+100000},location.origin)")
  page.wait_for_timeout(150)
  assert page.locator('.node.online').count()==0
  print('PASS: forged postMessage does not authorize a node')
  # Popup navigates to Scanner first-party login; no cross-site cookies or passwords sent to panel.
  with page.expect_popup() as pop:page.locator('[data-node="nc"] button').first.click()
  popup=pop.value;popup.wait_for_load_state()
  assert '/login' in popup.url
  popup.locator('[name=username]').fill('browser-test');popup.locator('[name=password]').fill('test-only-password')
  popup.locator('button[type=submit]').click()
  page.locator('[data-node="nc"].online').wait_for(timeout=15000)
  assert page.locator('[data-node="nc"] .task').count()==3
  assert page.locator('[data-node="nc"] .dot.passed').count()==1
  assert page.locator('[data-node="nc"] .dot.failed').count()==1
  assert page.locator('[data-node="nc"] .name img').count()==0
  for node in ('hd','gb1','gb2'):
   with page.expect_popup():page.locator(f'[data-node="{node}"] button').first.click()
   page.locator(f'[data-node="{node}"].online').wait_for(timeout=15000)
  assert page.locator('.node.online').count()==4
  print('PASS: login popup, reuse first-party login, four independent grants, real CORS fetch, progress/segments, escaped filenames')
  # Refresh must restore all grants without opening any login windows.
  popup_events=[]
  page.on('popup', lambda popup: popup_events.append(popup))
  page.reload()
  page.wait_for_function("document.querySelectorAll('.node.online').length === 4", timeout=10000)
  assert not popup_events, 'Refresh must not prompt for login again'
  grants=page.evaluate("JSON.parse(localStorage.getItem('scanner.monitorGrants.v1'))")
  assert len(grants)==4
  assert all(set(g)=={'origin','token','expiresAt'} for g in grants.values())
  assert page.evaluate('sessionStorage.length')==0
  print('PASS: refresh restores four scoped grants, no login popup or stored credentials')
  page.screenshot(path=str(Path(tempfile.gettempdir())/'panel-desktop.png'),full_page=True)
  page.set_viewport_size({'width':390,'height':844})
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  page.screenshot(path=str(Path(tempfile.gettempdir())/'panel-mobile.png'),full_page=True)
  # Invalidate one grant only; other nodes keep showing data.
  page.route(urls[1]+'/api/monitor/snapshot',lambda route:route.fulfill(status=401,headers={'Access-Control-Allow-Origin':origin},json={'code':401}))
  page.locator('#refresh').click();page.locator('[data-node="hd"].expired').wait_for(timeout=15000)
  assert page.locator('.node.online').count()==3
  assert page.locator('[data-node="hd"] .task').count()==0
  # Authorization errors, disconnected popup and offline are distinct.
  page.route(urls[2]+'/api/monitor/snapshot',lambda route:route.abort())
  page.locator('#refresh').click();page.locator('[data-node="gb1"].offline').wait_for(timeout=15000)
  assert page.locator('[data-node="gb1"].stale').count()==1
  page.locator('[data-node="gb2"] button',has_text='断开').click()
  assert page.locator('[data-node="gb2"].login').count()==1
  assert 'hd' not in page.evaluate("JSON.parse(localStorage.getItem('scanner.monitorGrants.v1'))")
  assert 'gb2' not in page.evaluate("JSON.parse(localStorage.getItem('scanner.monitorGrants.v1'))")
  # A newly opened page uses the same grants, no authentication popup needed.
  reopened=ctx.new_page()
  reopened.goto(origin+'/spd')
  reopened.locator('[data-node="nc"].online').wait_for(timeout=10000)
  assert reopened.locator('[data-node="hd"].login').count()==1
  assert reopened.locator('[data-node="gb2"].login').count()==1
  reopened.close()
  # Expired and wrong-origin stored grants are rejected on reload.
  page.evaluate("""() => {
    const key='scanner.monitorGrants.v1', grants=JSON.parse(localStorage.getItem(key));
    grants.nc.expiresAt=Date.now()-1;
    grants.gb1.origin='https://wrong.example';
    localStorage.setItem(key,JSON.stringify(grants));
  }""")
  page.reload()
  assert page.locator('.node.login').count()==4
  assert page.evaluate("localStorage.getItem('scanner.monitorGrants.v1')") is None
  page.evaluate("localStorage.setItem('scanner.monitorGrants.v1','{broken-json')")
  page.reload()
  assert page.locator('.node.login').count()==4
  # Disabled browser storage must still allow login in this page, with an honest warning.
  blocked=ctx.new_page()
  blocked.add_init_script("""for(const method of ['getItem','setItem','removeItem']) {
    Storage.prototype[method]=()=>{throw new DOMException('Blocked','SecurityError')};
  }""")
  blocked.goto(origin+'/spd')
  with blocked.expect_popup():blocked.locator('[data-node="nc"] button').first.click()
  blocked.locator('[data-node="nc"].online').wait_for(timeout=10000)
  assert '禁止存储' in blocked.locator('#summary').inner_text()
  blocked.close()
  assert not errors,errors
  print('PASS: mobile width, 401 clears saved grant, disconnect stays disconnected, reopening restores, expired/mismatched/corrupt grants removed, blocked storage fallback, no JS errors')
  print('screenshots:',str(Path(tempfile.gettempdir())/'panel-desktop.png'),str(Path(tempfile.gettempdir())/'panel-mobile.png'))
  browser.close()
finally:
 for s in servers:s.shutdown()
 panel.shutdown()
