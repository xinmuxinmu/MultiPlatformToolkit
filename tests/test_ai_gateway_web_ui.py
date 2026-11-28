"""Offline tests for connection guidance, model selection and key privacy."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "src/web_ui/modules/ai_gateway/ui.html"


class PageIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for name, value in attrs if name == "id")


class GatewayWebStaticTests(unittest.TestCase):
    def test_controls_have_unique_targets_and_no_html_string_injection(self):
        source = PAGE.read_text(encoding="utf-8")
        parser = PageIds()
        parser.feed(source)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for control in ("base", "chatEndpoint", "apiFormat", "modelSearch",
                        "modelProvider", "modelRows", "exampleModel",
                        "copyExample", "copyCurl", "connectionStatus"):
            self.assertIn(control, parser.ids)
        self.assertNotRegex(source, r"\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML")
        self.assertIn("&lt;API_KEY&gt;", source)
        self.assertIn("完整模型 ID", source)


HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.value='';
    this.disabled=false;this.textContent='';this.className='';this.attributes={};this.style={};}
  append(...nodes){this.children.push(...nodes);if(this.tagName==='SELECT'&&!this.value)this.value=nodes[0]?.value||'';}
  replaceChildren(...nodes){const old=this.value;this.children=[];this.value='';this.append(...nodes);
    if(this.tagName==='SELECT'&&nodes.some(node=>node.value===old))this.value=old;}
  setAttribute(name,value){this.attributes[name]=value;}
  addEventListener(name,fn){this['on'+name]=fn;}
  set innerHTML(value){throw new Error('unsafe innerHTML assignment');}
  set outerHTML(value){throw new Error('unsafe outerHTML assignment');}
  click(){this.clicked=true;if(this.onclick)return this.onclick();}
  remove(){this.removed=true;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const ids={};
for(const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/gi)){ids[match[2]]=new Element(match[1]);}
ids.apiFormat.value='openai-chat-completions';
const get=id=>{assert(ids[id],'unknown DOM target '+id);return ids[id];};
const texts=node=>[node.textContent,...node.children.map(texts)].join(' ');
const fixture={base_url:'http://127.0.0.1:8765/v1',
  endpoints:{models:'http://127.0.0.1:8765/v1/models',chat_completions:'http://127.0.0.1:8765/v1/chat/completions',messages:'http://127.0.0.1:8765/v1/messages'},
  authentication:{header:'Authorization',value_template:'Bearer <API_KEY>',alternative_header:'x-api-key'},
  client_fields:{api_format:'OpenAI Chat Completions',base_url:'http://127.0.0.1:8765/v1',
    full_url_help:'完整 URL 填完整请求端点，不要重复追加 /v1',model_id_help:'必须填写平台/模型',api_key_help:'使用统一 API Key'},
  summary:{model_count:3,chat_provider_count:2,enabled_account_count:2},
  models:[{id:'trae/Seed',provider:'trae',provider_name:'Trae CN',api_formats:['openai-chat-completions'],input_mode:'single_user_text',tools_supported:false,agent_compatible:false,usage_note:'Trae 仅支持单轮纯文本'},
    {id:'kimi/alpha',provider:'kimi',provider_name:'Kimi',api_formats:['openai-chat-completions','anthropic-messages'],input_mode:'messages',tools_supported:null,agent_compatible:null,usage_note:'工具能力取决于上游'},
    {id:'kimi/<img src=x onerror=boom>',provider:'kimi',provider_name:'Kimi',api_formats:['openai-chat-completions'],input_mode:'messages',tools_supported:null,agent_compatible:null,usage_note:'<script>boom</script>'}],
  providers:[{id:'trae',name:'Trae CN',model_count:1},{id:'kimi',name:'Kimi',model_count:2}],
  client_guidance:['从目录复制完整模型 ID','单轮平台不适合 Agent'],
  example:{method:'POST',url:'http://127.0.0.1:8765/v1/chat/completions',headers:{Authorization:'Bearer <API_KEY>','Content-Type':'application/json'},body:{model:'trae/Seed',messages:[{role:'user',content:'Reply exactly OK'}],stream:true}}};
const stateFixture={base_url:fixture.base_url,providers:[
  {id:'trae',name:'Trae CN',capabilities:{chat:true,local_import:true,login:false}},
  {id:'kimi',name:'Kimi',capabilities:{chat:true,local_import:true,login:true}}],
  accounts:[{id:'account-a',provider:'trae',label:'<img src=x onerror=boom>',enabled:true}]};
const requests=[],clipboard=[],blobs=[];let mode='ok',pending=null;
const context=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag),body:new Element('body')},
  navigator:{clipboard:{writeText:async text=>clipboard.push(text)}},
  Blob:class{constructor(parts,options){this.parts=parts;this.options=options;blobs.push(this);}},
  URL:{createObjectURL:()=> 'blob:offline-test',revokeObjectURL(){}},
  setTimeout(){return 1;},clearTimeout(){},
  fetch:async(url,options)=>{requests.push({url,options});
    if(url.endsWith('/connection')){if(mode==='pending')await new Promise(resolve=>pending=resolve);
      if(mode==='error')return {ok:false,json:async()=>({error:{message:'offline catalogue unavailable'}})};
      return {ok:true,json:async()=>mode==='empty'?{...fixture,models:[],summary:{model_count:0,chat_provider_count:0,enabled_account_count:0}}:structuredClone(fixture)};}
    if(url.endsWith('/state'))return {ok:true,json:async()=>structuredClone(stateFixture)};
    if(url.endsWith('/key'))return {ok:true,json:async()=>({api_key:'explicitly-requested-test-key'})};
    if(url.includes('/accounts/'))return {ok:true,json:async()=>({ok:true})};
    throw new Error('unexpected request '+url);}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const run=code=>vm.runInContext(code,context);
const keyRequests=()=>requests.filter(request=>request.url.endsWith('/key'));
vm.runInContext(source,context);
'''


@unittest.skipUnless(shutil.which("node"), "Node.js is required for offline web UI checks")
class GatewayWebJsTests(unittest.TestCase):
    def run_js(self, checks):
        script = HARNESS + "\n(async()=>{await settle();\n" + checks + r'''
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(PAGE)],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_automatic_load_shows_connection_and_preserves_account_controls_without_key(self):
        self.run_js(r'''
assert.equal(requests.filter(r=>r.url.endsWith('/connection')).length,1);
assert.equal(get('base').textContent,fixture.base_url);
assert.equal(get('chatEndpoint').textContent,fixture.endpoints.chat_completions);
assert.equal(get('modelsEndpoint').textContent,fixture.endpoints.models);
assert.match(texts(get('clientGuidance')),/不要重复追加/);
assert.match(get('connectionSummary').textContent,/3 个模型/);
assert.equal(get('modelRows').children.length,3);
assert.match(texts(get('modelRows')),/<img src=x onerror=boom>/);
assert.match(texts(get('accounts')),/<img src=x onerror=boom>/);
assert.equal(get('login').disabled,true);assert.equal(get('import').disabled,false);
get('platform').value='kimi';get('platform').onchange();assert.equal(get('login').disabled,false);
const accountAction=get('accounts').children[0].children.at(-1).children[0];
await accountAction.onclick();
assert(requests.some(r=>r.url.endsWith('/accounts/account-a')&&r.options.method==='PATCH'));
assert.equal(keyRequests().length,0);
''')

    def test_search_platform_and_format_filters_select_compatible_full_ids(self):
        self.run_js(r'''
get('modelSearch').value='seed';get('modelSearch').oninput();
assert.equal(get('modelRows').children.length,1);assert.match(texts(get('modelRows')),/trae\/Seed/);
assert.match(get('selectedModelNote').textContent,/不适合 Agent/);
get('modelSearch').value='';get('modelProvider').value='kimi';get('modelProvider').onchange();
assert.equal(get('modelRows').children.length,2);
get('apiFormat').value='anthropic-messages';get('apiFormat').onchange();
assert.equal(get('chatEndpoint').textContent,fixture.endpoints.messages);
assert.equal(get('clientGuidance').children[0].textContent,'客户端 API 格式：Anthropic Messages');
assert.equal(get('modelRows').children.length,1);assert.match(texts(get('modelRows')),/kimi\/alpha/);
assert.equal(get('exampleModel').value,'kimi/alpha');
const request=run('exampleRequest()');assert.equal(request.body.max_tokens,256);
assert.equal(request.headers['x-api-key'],'<API_KEY>');
get('modelSearch').value='nothing-matches';get('modelSearch').oninput();
assert.match(texts(get('modelRows')),/没有匹配/);assert.equal(get('copyExample').disabled,true);
assert.equal(keyRequests().length,0);
''')

    def test_server_preferred_example_is_used_when_current_selection_disappears(self):
        self.run_js(r'''
fixture.example.body.model='kimi/alpha';run("selectedModelId=''");await run('loadConnection()');
assert.equal(get('exampleModel').value,'kimi/alpha');
assert.equal(run('exampleRequest()').body.model,'kimi/alpha');
assert.equal(keyRequests().length,0);
''')

    def test_copy_examples_keep_placeholder_key_and_follow_selected_model(self):
        self.run_js(r'''
get('exampleModel').value='kimi/alpha';get('exampleModel').onchange();
await get('copyModel').onclick();assert.equal(clipboard.at(-1),'kimi/alpha');
await get('copyExample').onclick();assert.equal(JSON.parse(clipboard.at(-1)).model,'kimi/alpha');
await get('copyCurl').onclick();const curl=clipboard.at(-1);
assert(curl.includes('Bearer <API_KEY>'));assert(curl.includes('kimi/alpha'));
assert(curl.includes(String.fromCharCode(92,10)));assert(!curl.includes('explicitly-requested-test-key'));
assert.equal(keyRequests().length,0);
assert.equal(run('shellQuote("a\'b")'),"'a'\"'\"'b'");
await get('copyBase').onclick();assert.equal(clipboard.at(-1),fixture.base_url);
await get('copyEndpoint').onclick();assert.equal(clipboard.at(-1),fixture.endpoints.chat_completions);
await get('key').onclick();assert.equal(keyRequests().length,1);
assert.equal(clipboard.at(-1),'explicitly-requested-test-key');
assert(!Object.values(ids).map(texts).join(' ').includes('explicitly-requested-test-key'));
''')

    def test_export_preserves_legacy_fields_and_adds_connection_metadata(self):
        self.run_js(r'''
assert.equal(keyRequests().length,0);await get('export').onclick();
assert.equal(keyRequests().length,1);const saved=JSON.parse(blobs.at(-1).parts.join(''));
assert.equal(saved.base_url,fixture.base_url);assert.equal(saved.api_key,'explicitly-requested-test-key');
assert.deepEqual(saved.models,fixture.models.map(model=>model.id));
assert.equal(saved.connection.endpoints.messages,fixture.endpoints.messages);
assert.equal(saved.connection.models[0].agent_compatible,false);
assert.equal(saved.connection.example.headers.Authorization,'Bearer <API_KEY>');
''')

    def test_loading_empty_and_error_states_disable_invalid_copy_and_export(self):
        self.run_js(r'''
mode='pending';const loading=run('loadConnection()');await settle();
assert.match(get('connectionStatus').textContent,/正在加载/);assert.equal(get('models').disabled,true);
mode='empty';pending();await loading;
assert.match(get('connectionStatus').textContent,/没有可用聊天模型/);
assert.match(texts(get('modelRows')),/暂无可用模型/);assert.equal(get('copyExample').disabled,true);
mode='error';await get('models').onclick();
assert.match(get('connectionStatus').textContent,/加载失败.*offline catalogue unavailable/);
assert.equal(get('modelStatus').textContent,'模型目录加载失败');
assert.equal(get('export').disabled,true);assert.equal(get('copyEndpoint').disabled,true);
await get('export').onclick();assert.equal(keyRequests().length,0);
assert.equal(blobs.length,0);
''')


if __name__ == "__main__":
    unittest.main()
