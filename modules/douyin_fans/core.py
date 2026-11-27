"""Read real Douyin circle pages without inventing task APIs or completion.

The screenshot is a reference checklist, never current account state. Only an
explicitly requested, unambiguously free check-in may be clicked and verified.
"""
from __future__ import annotations

import copy
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

from app.paths import DATA_ROOT, DOUYIN_CHAT_HOME

HOME = DATA_ROOT / "douyin_fans"
DEFAULT_CIRCLE_URL = "https://www.iesdouyin.com/share/microapp/?token=QzI5NEM4QkJEQjhDNzZqdHpmazQ2&share_channel=copy"
DEFAULT_CONFIG = {"artist": "封茗囧菌", "circle_url": DEFAULT_CIRCLE_URL, "account_id": ""}
_LOCK = threading.RLock()
_WORKER = None
_BUSY = False
_LAST_RESULT = None

REFERENCE_TASKS = [
    {"id": "listen", "title": "听 TA 的歌", "reference": "听歌 20 分钟；每 5 分钟 50 亲密度", "pattern": r"听.{0,8}(?:歌|音乐)", "kind": "playback", "reason": "需要真实播放；网页播放器时间只作观测，不等于平台累计任务时长，不上报或伪造时长。"},
    {"id": "comment", "title": "评论歌曲", "reference": "评论 4 次；每条 5 亲密度", "pattern": r"评论", "kind": "manual", "reason": "评论需要本人选择内容并在官方页面明确提交。"},
    {"id": "share", "title": "分享", "reference": "分享 2 次；每次 5 亲密度", "pattern": r"分享", "kind": "manual", "reason": "分享需要本人选择接收对象与渠道，程序不会自动发送。"},
    {"id": "publish", "title": "投稿", "reference": "投稿 1 条；审核后 60 亲密度", "pattern": r"投稿", "kind": "manual", "reason": "需要本人创作、发布并经平台审核，不能自动宣称完成。"},
    {"id": "discussion", "title": "圈子讨论", "reference": "讨论 1 条；审核后 10 亲密度", "pattern": r"(?:圈子.{0,5})?讨论", "kind": "manual", "reason": "需要本人撰写与提交讨论，审核结果以客户端为准。"},
    {"id": "invite", "title": "邀请好友签到", "reference": "邀请 2 人签到；每人 5 亲密度", "pattern": r"邀.{0,6}好友", "kind": "manual", "reason": "需要好友本人接受邀请并签到，程序不自动邀请或替他人签到。"},
    {"id": "light", "title": "点亮灯牌", "reference": "1 钻；100 亲密度（截图）", "pattern": r"灯牌", "kind": "paid", "reason": "涉及钻石消费，程序不执行。"},
    {"id": "heat_checkin", "title": "团热度打卡签到", "reference": "签到 5 亲密度", "pattern": r"(?:团热度|打卡|签到)", "kind": "checkin", "reason": "仅在真实官方网页明确显示免费签到按钮，并能刷新复查已签到时支持执行。"},
    {"id": "gift", "title": "礼物任务", "reference": "礼物 4000 钻（按截图文字记录）", "pattern": r"礼物", "kind": "paid", "reason": "涉及送礼与钻石消费，程序不执行。"},
    {"id": "live", "title": "观看直播", "reference": "观看 20 分钟 190；再看 5 分钟 +50（截图）", "pattern": r"(?:看|观看).{0,6}直播", "kind": "playback", "reason": "需要真实观看且由官方计时；不刷时长、不伪造完成或奖励。"},
]
_REFERENCE_BY_ID = {task["id"]: task for task in REFERENCE_TASKS}
_SIGN_LABELS = {"签到", "立即签到", "每日签到", "打卡签到", "签到打卡", "团热度打卡", "团热度打卡签到"}
_SIGNED_RE = re.compile(r"今日已签到|今日已打卡|已签到|签到成功|打卡成功")
_PAID_RE = re.compile(r"(?:\d\s*钻|钻石|金币|充值|购买|支付|兑换|送礼|礼物|灯牌|付费)")
_CHECKIN_CONTEXT_RE = re.compile(r"免费|团热度|每日签到|打卡签到|签到打卡|签到.{0,15}亲密|亲密.{0,15}签到")


def official_url(value: str, *, allow_empty=False) -> str:
    value = str(value or "").strip()
    if not value and allow_empty:
        return ""
    if any(ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("圈子链接包含不支持的字符")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        valid_host = any(host == domain or host.endswith("." + domain) for domain in ("douyin.com", "iesdouyin.com"))
        if parsed.scheme != "https" or not valid_host or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        if re.search(r"/(?:im|chat|messages?)(?:/|$)", unquote(parsed.path + "/" + parsed.fragment), re.I):
            raise ValueError("请提供官方圈子任务链接，不要填写聊天页面")
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("请提供"):
            raise
        raise ValueError("只接受 HTTPS 的 douyin.com / iesdouyin.com 官方圈子链接") from exc
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def shared_accounts():
    """Only account metadata is read here; no chat database or message page."""
    index = _read_json(DOUYIN_CHAT_HOME / "accounts" / "index.json", {})
    metas = index.get("accounts", {}) if isinstance(index, dict) else {}
    if not isinstance(metas, dict):
        return {"active": "", "accounts": []}
    accounts = [{"id": account_id, "name": str(meta.get("name") or account_id),
                 "uid": str(meta.get("uid") or ""), "status": str(meta.get("status") or "")}
                for account_id, meta in metas.items()
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) and isinstance(meta, dict)]
    return {"active": str(index.get("active") or ""), "accounts": accounts}


def load_config():
    value = _read_json(HOME / "config.json", {})
    config = dict(DEFAULT_CONFIG)
    if isinstance(value, dict):
        config.update({key: str(value.get(key) or default) for key, default in DEFAULT_CONFIG.items()})
    try:
        config["circle_url"] = official_url(config["circle_url"], allow_empty=True)
    except ValueError:
        config["circle_url"] = ""
    if config["account_id"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config["account_id"]):
        config["account_id"] = ""
    return config


def save_config(value):
    if not isinstance(value, dict):
        raise ValueError("配置需要 JSON 对象")
    with _LOCK:
        if _BUSY:
            raise ValueError("正在读取圈子页面，请结束后再保存配置")
        config = load_config()
        if "artist" in value:
            config["artist"] = str(value["artist"]).strip()[:80] or DEFAULT_CONFIG["artist"]
        if "circle_url" in value:
            config["circle_url"] = official_url(value["circle_url"], allow_empty=True)
        if "account_id" in value:
            account_id = str(value["account_id"] or "")
            valid_ids = {account["id"] for account in shared_accounts()["accounts"]}
            if account_id and account_id not in valid_ids:
                raise ValueError("所选共享抖音账号不存在，请刷新账号列表")
            config["account_id"] = account_id
        _write_json(HOME / "config.json", config)
        return config


def _snapshot_path(config):
    identity = config["account_id"] + "\n" + config["artist"] + "\n" + config["circle_url"]
    return HOME / "snapshots" / (hashlib.sha256(identity.encode()).hexdigest()[:20] + ".json")


class _RenderDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.active = tag.lower() == "script" and dict(attrs).get("id") == "RENDER_DATA"

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def parse_microapp_share(html, source_url):
    """Decode only public SSR metadata; omit sharer Uid, Did and tracking fields."""
    source_url = official_url(source_url)
    parser = _RenderDataParser()
    parser.feed(html)
    raw = unescape("".join(parser.parts)).strip()
    for _ in range(3):
        if raw.startswith(("{", "[")):
            break
        raw = unquote(raw)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("官方分享页未提供可解析的小程序信息；链接可能已失效或需要在客户端打开") from exc
    if not isinstance(data, dict) or data.get("isRightToken") is False:
        raise ValueError("官方分享页提示链接无效，请重新从趣追圈子分享入口复制链接")
    share, app = data.get("shareInfo", {}), data.get("appInfo", {})
    if not isinstance(share, dict) or not isinstance(app, dict) or not share.get("AppId"):
        raise ValueError("官方分享页没有小程序标识，不能推断圈子任务接口")
    start_page = str(share.get("Query") or "")[:2048]
    for _ in range(2):
        decoded = unquote(start_page)
        if decoded == start_page:
            break
        start_page = decoded
    parsed = urlsplit(start_page)
    if parsed.scheme or parsed.netloc or ".." in parsed.path.split("/"):
        raise ValueError("官方页面返回了不支持的小程序路径")
    params = parse_qs(parsed.query)
    return {"source": "official_public_share_ssr", "resolved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_url": source_url, "app_name": str(app.get("Name") or data.get("title") or "")[:100],
            "circle_title": str(share.get("Title") or "")[:200], "app_id": str(share["AppId"])[:100],
            "host_app": str(data.get("appName") or "")[:50], "host_id": share.get("HostId"),
            "page_path": parsed.path, "start_page": start_page,
            "fandom_id": str(params.get("fandom_id", [""])[0])[:100],
            "music_aid": str(params.get("music_aid", [""])[0])[:100],
            "entry_type": "client_miniapp_share", "web_tasks_exposed": False,
            "note": "此入口是趣追小程序的官方分享落地页，显示“打开小程序”；没有在分享页提供任务进度或签到按钮。请用手机抖音/分享页对应宿主打开小程序完成任务，程序不会猜测私有接口。"}


def resolve_share_url(url):
    """Public, unauthenticated HTTPS GET; validate every redirect before sending."""
    import httpx
    current = official_url(url)
    with httpx.Client(timeout=15, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}) as client:
        for _ in range(5):
            response = client.get(current)
            if response.status_code in (301, 302, 303, 307, 308):
                current = official_url(urljoin(current, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("官方分享页过大，停止解析；请在客户端打开")
            return parse_microapp_share(response.text, current)
    raise ValueError("官方分享链接跳转次数过多，请复制直接圈子链接")


def _save_share_snapshot(config, entry):
    snapshot = {"observed_at": None, "tasks": initial_tasks(), "share_entry": entry,
                "page": {"entry_type": "client_miniapp_share", "matched_tasks": 0,
                         "note": entry["note"]}, "playback": []}
    _write_json(_snapshot_path(config), snapshot)


def initial_tasks():
    return [{**copy.deepcopy(task), "source": "user_screenshot", "status": "未同步",
             "observed_text": "", "progress": None, "manual_url": "", "can_auto_checkin": False}
            for task in REFERENCE_TASKS]


def state():
    config = load_config()
    snapshot = _read_json(_snapshot_path(config), {})
    with _LOCK:
        busy, result = _BUSY, copy.deepcopy(_LAST_RESULT)
    return {"ok": True, "config": config, "accounts": shared_accounts(), "running": busy,
            "reference": {"source": "user_screenshot", "circle": "封茗囧菌的圈子", "fans_at_capture": 1095,
                          "note": "任务与数值来自用户截图，未同步时不代表当前账号或当前平台规则。"},
            "tasks": snapshot.get("tasks") or initial_tasks(), "last_observed_at": snapshot.get("observed_at"),
            "page": snapshot.get("page", {}), "playback": snapshot.get("playback", []),
            "share_entry": snapshot.get("share_entry", {}),
            "last_result": result,
            "limitations": ["已默认填入用户提供的官方分享链接；分享页元数据不等于任务同步。",
                            "趣追小程序分享页只提供客户端入口，网页未展示的任务需在客户端完成。",
                            "评论、分享、投稿、讨论、邀请由本人在官方页面提交；钻石、礼物、灯牌不自动执行。",
                            "播放器的当前时间不等于官方累计任务时长，程序不刷时长或伪造任务完成。"]}


def _account_cookies(account_id):
    if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
        return []
    raw = _read_json(DOUYIN_CHAT_HOME / "accounts" / account_id / "cookies_backup.json", [])
    result = []
    if not isinstance(raw, list):
        return result
    for cookie in raw:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not any(domain == suffix or domain.endswith("." + suffix) for suffix in ("douyin.com", "iesdouyin.com")):
            continue
        expiry = cookie.get("expires", cookie.get("expiry", -1))
        if isinstance(expiry, (int, float)) and 0 < expiry < time.time():
            continue
        item = {key: cookie[key] for key in ("name", "value", "domain", "path", "secure", "httpOnly") if key in cookie}
        if not item.get("name") or "value" not in item:
            continue
        item.setdefault("path", "/")
        if isinstance(expiry, (int, float)) and expiry > 0:
            item["expires"] = expiry
        if cookie.get("sameSite") in ("Strict", "Lax", "None"):
            item["sameSite"] = cookie["sameSite"]
        result.append(item)
    return result


DOM_INSPECT = r"""(input) => {
  const specs=Array.isArray(input)?input:input.specs;
  const visible = el => {const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=='hidden'};
  const text = el => (el.innerText||'').replace(/\s+/g,' ').trim();
  const buttons = [...document.querySelectorAll('button,a,[role="button"]')];
  const nodes = [...document.querySelectorAll('li,article,section,div')].filter(visible);
  const rows = specs.map(spec => {
    const re=new RegExp(spec.pattern,'i');
    const choices=nodes.map(el=>({el,t:text(el)})).filter(x=>x.t.length>=4&&x.t.length<=550&&re.test(x.t));
    choices.sort((a,b)=>{
      const score=x=>(x.el.querySelector('button,a,[role="button"]')?1000:0)+(/\d+\s*\/\s*\d+/.test(x.t)?100:0)-x.t.length;
      return score(b)-score(a);
    });
    if(!choices.length)return {id:spec.id,found:false};
    const row=choices[0];
    const controls=buttons.map((el,index)=>({el,index})).filter(x=>row.el.contains(x.el)&&visible(x.el)).map(x=>({index:x.index,text:text(x.el),disabled:!!x.el.disabled||x.el.getAttribute('aria-disabled')==='true'}));
    const links=[...row.el.querySelectorAll('a[href]')].map(a=>a.href).slice(0,5);
    return {id:spec.id,found:true,text:row.t,controls,links};
  });
  const playback=[...document.querySelectorAll('audio,video')].filter(visible).map(el=>({kind:el.tagName.toLowerCase(),current_time:Number.isFinite(el.currentTime)?el.currentTime:null,duration:Number.isFinite(el.duration)?el.duration:null,paused:el.paused,ended:el.ended,playback_rate:el.playbackRate}));
  const body=text(document.body).slice(0,50000);
  return {title:document.title,rows,playback,login_prompt:/登录后|请先登录|扫码登录/.test(body),circle_marker:/圈子|粉丝团|亲密度|趣追/.test(body),artist_marker:!!input.artist&&body.includes(input.artist)};
}"""


def interpret_page(observed, config):
    """Pure interpretation: screenshot data is kept distinct from observed DOM."""
    tasks = initial_tasks()
    found_rows = {row["id"]: row for row in observed.get("rows", []) if row.get("found")}
    for task in tasks:
        row = found_rows.get(task["id"])
        if not row:
            task["status"] = "网页未找到"
            continue
        excerpt = str(row.get("text") or "")[:550]
        valid_checkin_context = (bool(observed.get("circle_marker")) and bool(observed.get("artist_marker"))
                                 and bool(_CHECKIN_CONTEXT_RE.search(excerpt))
                                 and not _PAID_RE.search(excerpt)
                                 and not re.search(r"邀请|好友|助力", excerpt))
        task.update(source="official_web_snapshot", observed_text=excerpt, status="网页已识别，完成状态未知")
        progress = re.search(r"(\d+)\s*/\s*(\d+)", excerpt)
        if progress:
            task["progress"] = {"current": int(progress[1]), "target": int(progress[2])}
        if _SIGNED_RE.search(excerpt) and task["id"] == "heat_checkin" and valid_checkin_context:
            task["status"] = "网页显示已签到"
        elif re.search(r"已完成|今日已完成|已领取", excerpt):
            task["status"] = "网页显示已完成"
        elif re.search(r"去完成|未完成|立即完成", excerpt):
            task["status"] = "网页显示待完成"
        for url in row.get("links", []):
            try:
                task["manual_url"] = official_url(url)
                break
            except ValueError:
                continue
        if (task["id"] == "heat_checkin" and valid_checkin_context
                and not _PAID_RE.search(excerpt) and not observed.get("login_prompt")):
            controls = [control for control in row.get("controls", [])
                        if str(control.get("text") or "").strip() in _SIGN_LABELS and not control.get("disabled")]
            if len(controls) == 1 and observed.get("circle_marker"):
                task["can_auto_checkin"] = True
                task["button_index"] = controls[0]["index"]
    return {"observed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tasks": tasks,
            "page": {"title": str(observed.get("title") or "")[:200],
                     "login_prompt": bool(observed.get("login_prompt")),
                     "circle_marker": bool(observed.get("circle_marker")),
                     "artist_marker": bool(observed.get("artist_marker")),
                     "matched_tasks": len(found_rows)},
            "playback": [{**media, "note": "网页播放器当前状态；不代表平台累计任务时长或已完成"}
                         for media in observed.get("playback", [])]}


def _inspect(page, config):
    official_url(page.url)
    observed = page.evaluate(DOM_INSPECT, {"artist": config["artist"],
                             "specs": [{"id": task["id"], "pattern": task["pattern"]} for task in REFERENCE_TASKS]})
    snapshot = interpret_page(observed, config)
    if not snapshot["page"]["circle_marker"]:
        snapshot["page"]["note"] = "当前网页没有可确认的圈子任务标记；可能需要抖音客户端或登录。"
    return snapshot


class BrowserWorker(threading.Thread):
    """Own all Playwright objects on one thread, keeping a visible login window."""
    def __init__(self):
        super().__init__(name="douyin-fans-browser", daemon=True)
        self.jobs = queue.Queue()
        self.context = None
        self.page = None
        self.identity = None

    def _ensure_browser(self, playwright, config):
        identity = config["account_id"] or "local"
        if self.context is not None and self.identity != identity:
            self.context.close()
            self.context = self.page = None
        if self.context is None:
            from modules.signin.core import find_browser
            browser = find_browser()
            options = {"headless": False, "viewport": {"width": 1200, "height": 850}, "locale": "zh-CN"}
            if browser:
                options["executable_path"] = browser
            profile = HOME / "profiles" / identity
            profile.mkdir(parents=True, exist_ok=True)
            self.context = playwright.chromium.launch_persistent_context(str(profile), **options)
            self.identity = identity
            cookies = _account_cookies(config["account_id"])
            if cookies:
                self.context.add_cookies(cookies)
            self.context.on("close", self._closed)
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()
            self.page.route("**/*", self._route)
        return self.page

    def _closed(self, *args):
        self.context = self.page = None

    def _route(self, route):
        request = route.request
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            try:
                official_url(request.url)
            except ValueError:
                route.abort()
                return
        route.continue_()

    def _navigate(self, page, url):
        page.goto(official_url(url), wait_until="domcontentloaded", timeout=45000)
        official_url(page.url)  # Validate short-link redirects as well.
        page.bring_to_front()

    def _perform(self, playwright, action, config, task_id):
        is_share = "/share/microapp" in urlsplit(config["circle_url"]).path
        if action == "resolve" or (is_share and action in ("refresh", "checkin")):
            try:
                entry = resolve_share_url(config["circle_url"])
            except ValueError as exc:
                return {"status": "share_unavailable", "verified": False, "message": str(exc)}
            _save_share_snapshot(config, entry)
            return {"status": "manual_required" if action == "checkin" else "client_miniapp_entry",
                    "verified": False,
                    "message": f"已解析官方入口：{entry['app_name']} · {entry['circle_title']}。分享页不提供任务或签到操作，请在客户端打开小程序；任务仍为未同步。"}
        page = self._ensure_browser(playwright, config)
        if action == "login":
            self._navigate(page, "https://www.douyin.com/")
            return {"status": "manual_login", "verified": False,
                    "message": "请在圈子专用浏览器中完成抖音登录并核对账号，完成后打开圈子并刷新任务。登录态保留在本机圈子 profile。"}
        url = config["circle_url"]
        if action == "open-task":
            snapshot = _read_json(_snapshot_path(config), {})
            task = next((item for item in snapshot.get("tasks", []) if item.get("id") == task_id), None)
            if task and task.get("manual_url"):
                url = official_url(task["manual_url"])
        self._navigate(page, url)
        if action in ("open", "open-task"):
            return {"status": "opened", "verified": False,
                    "message": "已打开官方页面，请核对圈子和账号。评论、分享、投稿、讨论、邀请需本人在页面中提交；付费任务不自动执行。"}
        page.wait_for_timeout(1200)
        snapshot = _inspect(page, config)
        if action == "refresh":
            _write_json(_snapshot_path(config), snapshot)
            count = snapshot["page"]["matched_tasks"]
            return {"status": "observed" if count else "web_unavailable", "verified": False,
                    "message": f"读取到 {count} 类任务的网页文字；状态仅为本次网页快照。" if count else "未找到圈子任务，可能仅客户端开放，请在抖音客户端核对。"}
        if action != "checkin":
            raise ValueError("不支持的任务操作")
        task = next(item for item in snapshot["tasks"] if item["id"] == "heat_checkin")
        if task["status"] == "网页显示已签到":
            _write_json(_snapshot_path(config), snapshot)
            return {"status": "already", "verified": True, "message": "官方圈子网页显示今日已签到。"}
        if not task["can_auto_checkin"]:
            _write_json(_snapshot_path(config), snapshot)
            return {"status": "manual_required", "verified": False,
                    "message": "没有唯一且明确免费的签到按钮，未执行点击。请在官方客户端/网页手动核对；不会执行礼物、钻石或灯牌任务。"}
        control = page.locator('button,a,[role="button"]').nth(task["button_index"])
        if not control.is_visible() or not control.is_enabled() or control.inner_text().strip() not in _SIGN_LABELS:
            return {"status": "manual_required", "verified": False, "message": "签到按钮已变化，未执行点击，请刷新后核对。"}
        control.click(timeout=10000)
        page.wait_for_timeout(1500)
        # Reload the official page to verify persisted state, not only a toast.
        self._navigate(page, config["circle_url"])
        page.wait_for_timeout(1200)
        after = _inspect(page, config)
        _write_json(_snapshot_path(config), after)
        checked = next(item for item in after["tasks"] if item["id"] == "heat_checkin")
        verified = checked["status"] == "网页显示已签到"
        return {"status": "success" if verified else "unconfirmed", "verified": verified,
                "message": "签到后刷新官方网页，已确认显示已签到。" if verified else "已点击免费签到，但刷新后未能确认已签到，不记为成功，请在官方页面核对。"}

    def run(self):
        global _BUSY, _LAST_RESULT
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                while True:
                    action, config, task_id = self.jobs.get()
                    try:
                        result = self._perform(playwright, action, config, task_id)
                    except Exception as exc:
                        result = {"status": "error", "verified": False,
                                  "message": "官方页面操作未完成，请检查圈子链接、网页是否支持和登录状态。",
                                  "error_type": type(exc).__name__}
                    with _LOCK:
                        _LAST_RESULT = result
                        _BUSY = False
        except Exception as exc:
            with _LOCK:
                _LAST_RESULT = {"status": "error", "verified": False,
                                "message": "Python Playwright 不可用，请安装依赖和浏览器后重试。",
                                "error_type": type(exc).__name__}
                _BUSY = False


def start(action, task_id=""):
    global _WORKER, _BUSY, _LAST_RESULT
    if action not in ("login", "open", "refresh", "checkin", "open-task", "resolve"):
        raise ValueError("不支持的操作")
    config = load_config()
    if action != "login" and not config["circle_url"]:
        raise ValueError("请先从抖音/趣追圈子分享入口复制官方圈子任务链接并保存；截图无法确定实际地址")
    if action == "open-task":
        task = _REFERENCE_BY_ID.get(task_id)
        if task is None:
            raise ValueError("未知任务")
        if task["kind"] == "paid":
            raise ValueError("钻石、礼物和灯牌任务不提供自动执行或消费入口")
    with _LOCK:
        if _BUSY:
            raise ValueError("已有圈子浏览器操作在进行，请稍后刷新状态")
        _BUSY = True
        _LAST_RESULT = {"status": "running", "verified": False, "message": "正在操作官方圈子页面…"}
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = BrowserWorker()
            _WORKER.start()
        _WORKER.jobs.put((action, copy.deepcopy(config), task_id))
    return {"ok": True, "started": True, "action": action}
