"""Short lived browser CAPTCHA proofs for the native SMS form."""
from __future__ import annotations

import json
import secrets
import threading
import time

from modules.heybox import core

_LOCK = threading.Lock()
_BRIDGES: dict[str, dict] = {}
TTL = 5 * 60
MAX_BRIDGES = 16


def _matches(entry: dict, current: dict) -> bool:
    return (entry["expires_at"] > time.time()
            and current.get("stage") == "captcha"
            and entry["captcha_id"] == current.get("captcha_id")
            and entry["captcha_for"] == current.get("captcha_for"))


def open_bridge(captcha_id: str, captcha_for: str) -> dict:
    current = core.app_login_status()
    if (not captcha_id or current.get("stage") != "captcha"
            or captcha_id != current.get("captcha_id")
            or captcha_for not in ("send", "verify")
            or captcha_for != current.get("captcha_for")):
        raise core.HeyboxError("人机验证已过期，请重新操作")
    app_id = str(current.get("captcha_app_id") or "")
    if not app_id.isascii() or not app_id.isdigit() or len(app_id) > 32:
        raise core.HeyboxError("平台未返回有效的人机验证信息，请重新操作")
    with _LOCK:
        for token, entry in list(_BRIDGES.items()):
            if not _matches(entry, current):
                del _BRIDGES[token]
        # Reopening the same challenge keeps the active browser's callback valid.
        token = next((token for token, entry in _BRIDGES.items()
                      if entry["captcha_id"] == captcha_id), None)
        if token is None:
            while len(_BRIDGES) >= MAX_BRIDGES:
                del _BRIDGES[next(iter(_BRIDGES))]
            token = secrets.token_urlsafe(32)
            remaining = max(1, int(current.get("expires_in") or TTL))
            _BRIDGES[token] = {
                "captcha_id": captcha_id, "captcha_for": captcha_for,
                "app_id": app_id, "expires_at": time.time() + min(TTL, remaining),
                "proof": None,
            }
    return {"ok": True, "bridge_id": token,
            "url": "/heybox/app-login/captcha/" + token}


def bridge_page(token: str) -> str | None:
    current = core.app_login_status()
    with _LOCK:
        entry = _BRIDGES.get(token)
        if not entry or not _matches(entry, current):
            _BRIDGES.pop(token, None)
            return None
        app_id = entry["app_id"]
    return _PAGE.replace("__APP_ID__", json.dumps(app_id))


def submit_proof(token: str, body: dict) -> dict:
    ticket, randstr = body.get("ticket"), body.get("randstr")
    if (not isinstance(ticket, str) or not isinstance(randstr, str)
            or not 1 <= len(ticket) <= 2048 or not 1 <= len(randstr) <= 256
            or any(ord(char) < 32 for char in ticket + randstr)):
        raise core.HeyboxError("人机验证结果无效，请重试")
    current = core.app_login_status()
    with _LOCK:
        entry = _BRIDGES.get(token)
        if not entry or not _matches(entry, current):
            _BRIDGES.pop(token, None)
            raise core.HeyboxError("人机验证已过期，请回到软件重新操作")
        if entry["proof"] is not None:
            raise core.HeyboxError("验证结果已提交，请回到软件继续")
        entry["proof"] = {"ticket": ticket, "randstr": randstr}
    return {"ok": True, "msg": "人机验证已完成，请返回软件继续"}


def take_result(token: str) -> dict:
    current = core.app_login_status()
    with _LOCK:
        entry = _BRIDGES.get(token)
        if not entry or not _matches(entry, current):
            _BRIDGES.pop(token, None)
            return {"ok": True, "stage": "expired"}
        if entry["proof"] is None:
            return {"ok": True, "stage": "waiting"}
        proof = entry["proof"]
        del _BRIDGES[token]
        return {"ok": True, "stage": "ready", "captcha": proof}


_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>小黑盒人机验证</title>
<style>body{margin:0;background:#f5f7fb;color:#22304a;font:16px/1.7 system-ui,sans-serif}
main{max-width:480px;margin:12vh auto;padding:28px;background:white;border-radius:16px}
h1{font-size:22px}button{border:0;border-radius:8px;padding:10px 20px;background:#326cdb;color:white;font:inherit}
button:disabled{opacity:.5}#status{min-height:3.5em}</style></head>
<body><main><h1>小黑盒人机验证</h1><p>完成验证后，请返回软件继续短信登录。</p>
<p id="status" role="status">正在加载验证组件…</p><button id="retry" disabled>开始验证</button></main>
<script>
const status=document.getElementById('status'), button=document.getElementById('retry');
let loading=null, busy=false, submitted=false;
function load(){
  if(window.TencentCaptcha) return Promise.resolve();
  if(loading) return loading;
  loading=new Promise((resolve,reject)=>{
    const script=document.createElement('script');
    script.src='https://turing.captcha.qcloud.com/TCaptcha.js';
    script.onload=()=>window.TencentCaptcha?resolve():reject(new Error('验证组件不可用'));
    script.onerror=()=>{loading=null;script.remove();reject(new Error('验证组件加载失败，请重试'));};
    document.head.appendChild(script);
  });
  return loading;
}
async function start(){
  if(busy||submitted) return;
  busy=true;button.disabled=true;status.textContent='请在弹窗中完成人机验证。';
  try{
    await load();
    const captcha=new window.TencentCaptcha(__APP_ID__,async result=>{
      if(!result||result.ret!==0||!result.ticket||!result.randstr){
        busy=false;button.disabled=false;status.textContent='验证未完成，请重试。';return;
      }
      status.textContent='正在提交验证结果…';
      try{
        const response=await fetch(location.pathname,{method:'POST',cache:'no-store',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ticket:result.ticket,randstr:result.randstr})});
        const data=await response.json();
        if(!response.ok||!data.ok) throw new Error(data.error||'提交失败，请重试');
        submitted=true;status.textContent=data.msg;button.textContent='验证已完成';
      }catch(error){status.textContent=error.message||'提交失败，请重试';}
      finally{busy=false;button.disabled=submitted;}
    });
    captcha.show();
  }catch(error){loading=null;busy=false;button.disabled=false;status.textContent=error.message||'验证组件加载失败，请重试';}
}
button.addEventListener('click',start);start();
</script></body></html>'''
