"""QQ settings regression tests without account files, network, or real tasks."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import QApplication, QCheckBox, QTimeEdit

from app.desktop.qq_selection import QQSelectionWidget
from app.desktop.task_components import TaskSettingsEditor, overview_data


def state(*, automatic=True):
    return {
        "music": {"auto_all_free": automatic, "medal_selected_ids": ["one"],
                  "medal_task_types": ["browse"], "schedule_enabled": False,
                  "schedule_time": "09:30", "card_draw_enable": False},
        "cached": {"medal": {"medals": [
            {"id": "one", "name": "选中歌手", "tasks": [
                {"type": "browse", "name": "浏览", "capability": "auto"},
                {"type": "newly_supported", "name": "新增免费项", "capability": "auto"},
                {"type": "manual", "name": "需客户端", "capability": "manual", "auto": True},
                {"type": "paid", "name": "消费项", "capability": "paid"},
            ]},
            {"id": "two", "name": "未选歌手", "tasks": []},
        ]}},
    }


class QQSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = QQSelectionWidget()

    def tearDown(self):
        self.widget.deleteLater()
        self.application.processEvents()

    def task(self, typ):
        return next(self.widget.tasks.topLevelItem(i)
                    for i in range(self.widget.tasks.topLevelItemCount())
                    if self.widget.tasks.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == typ)

    def test_automatic_mode_uses_dynamic_types_and_preserves_artist_selection(self):
        current = state()
        self.widget.set_state(current)
        result = self.widget.update_config(current["music"])
        self.assertTrue(result["auto_all_free"])
        self.assertIsNone(result["medal_task_types"])
        self.assertEqual(result["medal_selected_ids"], ["one"])
        self.assertEqual(self.task("newly_supported").checkState(0), Qt.CheckState.Checked)
        self.assertFalse(self.widget.select_auto.isEnabled())
        self.assertFalse(self.task("browse").flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(current["music"]["medal_task_types"], ["browse"])

    def test_manual_capability_is_never_promoted_by_old_auto_flag(self):
        self.widget.set_state(state())
        self.assertEqual(self.task("manual").text(1), "需手动")
        self.assertEqual(self.task("manual").checkState(0), Qt.CheckState.Unchecked)
        self.assertFalse(self.task("paid").flags() & Qt.ItemFlag.ItemIsEnabled)

    def test_custom_mode_persists_explicit_empty_selection(self):
        current = state(automatic=False)
        self.widget.set_state(current)
        self.assertTrue(self.widget.tasks.isEnabled())
        self.assertTrue(self.task("browse").flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(self.task("browse").checkState(0), Qt.CheckState.Checked)
        self.assertEqual(self.task("newly_supported").checkState(0), Qt.CheckState.Unchecked)
        self.widget._check_all(self.widget.tasks, False)
        result = self.widget.update_config(current["music"])
        self.assertFalse(result["auto_all_free"])
        self.assertEqual(result["medal_task_types"], [])
        self.assertEqual(result["medal_selected_ids"], ["one"])

    def test_auto_toggle_and_polling_preserve_unsaved_artist_selection(self):
        current = state(automatic=False)
        self.widget.set_state(current)
        self.widget.medals.topLevelItem(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.widget.auto_all_free.setChecked(True)
        self.assertEqual(self.task("newly_supported").checkState(0), Qt.CheckState.Checked)
        self.widget.set_state(state())
        result = self.widget.update_config(current["music"])
        self.assertEqual(result["medal_selected_ids"], [])
        self.assertTrue(result["auto_all_free"])
        self.assertIsNone(result["medal_task_types"])

    def test_schedule_controls_round_trip_and_do_not_expose_secrets(self):
        editor = TaskSettingsEditor("/qq")
        config = {**state()["music"], "cookie": "offline-secret", "sign_enable": True}
        editor.set_config(config)
        enabled = editor.findChild(QCheckBox, "config_schedule_enabled")
        at = editor.findChild(QTimeEdit, "config_schedule_time")
        self.assertIsNotNone(enabled)
        self.assertIsNotNone(at)
        draw = editor.findChild(QCheckBox, "config_card_draw_enable")
        self.assertIsNotNone(draw)
        self.assertFalse(draw.isChecked())
        self.assertIn("1 次", draw.toolTip())
        enabled.setChecked(True)
        at.setTime(QTime(7, 5))
        result = editor.value()
        self.assertTrue(result["schedule_enabled"])
        self.assertEqual(result["schedule_time"], "07:05")
        self.assertEqual(result["cookie"], "offline-secret")
        self.assertEqual(result["medal_selected_ids"], ["one"])
        self.assertNotIn("offline-secret", str(overview_data("/qq", {"music": result})))
        editor.deleteLater()


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the offline web UI test")
class QQWebUiTests(unittest.TestCase):
    def test_browser_selection_and_failed_save_block_execution(self):
        # Execute the page's actual JS with a small DOM/fetch substitute. No server is started.
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
let source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
source = source.slice(0, source.lastIndexOf('(async () => {'));
class Element {
  constructor(){this.checked=false;this.value='';this.disabled=false;this.style={};this.checks=[];
    this.classList={add(){},toggle(){},remove(){}};this.valid=true;}
  set innerHTML(value){this.html=value;this.checks=[...value.matchAll(/<input\b([^>]*)>/g)].map(m=>{
    const attrs=m[1];return {value:(attrs.match(/value="([^"]*)"/)||[])[1],checked:/\schecked(?:\s|$)/.test(attrs),
      disabled:/\sdisabled(?:\s|$)/.test(attrs),dataset:{auto:(attrs.match(/data-auto="([^"]*)"/)||[])[1]}};});}
  get innerHTML(){return this.html||'';}
  querySelectorAll(selector){return this.checks.filter(x=>!selector.includes(':not(:disabled)')||!x.disabled);}
  checkValidity(){return this.valid;}
}
const elements={};const get=id=>elements[id]||(elements[id]=new Element());
const requests=[];let rejectSave=false;
const context=vm.createContext({location:{search:'',hash:''},document:{body:get('body'),
  getElementById:get,querySelectorAll(selector){const m=selector.match(/^#(\w+)/);return m?get(m[1]).querySelectorAll(selector):[];}},
  setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){},
  fetch:async(url,options)=>{requests.push({url,options});return {ok:!rejectSave,json:async()=>rejectSave?{ok:false,error:'配置未保存'}:{ok:true}};}});
vm.runInContext(source,context);
context.fixture={music:{auto_all_free:true,medal_task_types:['browse'],medal_selected_ids:['one'],schedule_time:'09:30'},
  cached:{medal:{medals:[{id:'one',name:'选择的歌手',tasks:[
    {type:'browse',name:'浏览',capability:'auto'},
    {type:'newly_supported',name:'新增任务',capability:'auto'},
    {type:'manual',name:'客户端任务',capability:'manual',auto:true},
    {type:'paid',name:'消费',capability:'paid'}]},
    {id:'two',name:'未选歌手',tasks:[]}]}}};
(async()=>{
  vm.runInContext('state=fixture;fillForm();paintState();',context);
  assert.equal(get('taskPick').checks.find(x=>x.value==='newly_supported').checked,true);
  assert.equal(get('taskPick').checks.find(x=>x.value==='manual').checked,false);
  assert.equal(await vm.runInContext('saveAll(true,false)',context),true);
  let saved=JSON.parse(requests.at(-1).options.body).music;
  assert.equal(saved.auto_all_free,true);assert.equal(saved.medal_task_types,null);
  assert.equal(saved.card_draw_enable,false);
  assert.deepEqual(saved.medal_selected_ids,['one']);
  vm.runInContext('setAutoTaskPicks()',context);
  assert.equal(await vm.runInContext('saveAll(true,false)',context),true);
  saved=JSON.parse(requests.at(-1).options.body).music;
  assert.equal(saved.auto_all_free,false);assert.deepEqual(saved.medal_task_types,['browse','newly_supported']);
  const before=requests.length;get('schedule_time').valid=false;
  await vm.runInContext('run()',context);assert.equal(requests.length,before);
  get('schedule_time').valid=true;rejectSave=true;
  await vm.runInContext('run()',context);assert.equal(requests.length,before+1);
  assert.equal(requests.at(-1).url,'/qq/api/save');
  console.log('web UI mode, artist, capability and failed-save checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(ROOT / "src/web_ui/modules/qq_tasks/ui.html")],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
