"""Node/DOM-заглушка: не настоящий браузерный E2E. Node не обязателен."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from panel_page import PANEL_HTML

HARNESS=r"""
const vm=require('vm');
const assert=require('assert/strict');
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag='div') {
    this.tag=tag;this.children=[];this._text='';this.value='';this.disabled=false;
    this.listeners={};this.style={};this.dataset={};this.className='';
    this.classList={toggle:()=>{},contains:()=>false,add:()=>{}};
  }
  set textContent(text){this._text=String(text);this.children=[];}
  get textContent(){return this._text;}
  append(...nodes){this.children.push(...nodes);}
  addEventListener(name,handler){this.listeners[name]=handler;}
  focus(){} removeAttribute(){} remove(){} click(){}
}
const nodes={};
const doc={getElementById:(id)=>nodes[id]||(nodes[id]=new Element()),
           createElement:(tag)=>new Element(tag),body:new Element()};
const requests=[];
const context=vm.createContext({console,document:doc,
  localStorage:{getItem:()=>'',setItem:()=>{},removeItem:()=>{}},
  window:{addEventListener:()=>{}},setInterval:()=>{},setTimeout:()=>{},
  AbortController,Uint8Array,URL,Blob,
  fetch:async(path,options)=>{
    requests.push({path,options});
    return {status:200,ok:true,json:async()=>path==='/api/status'?context.fixture:{ok:true}};
  }});
vm.runInContext(input.script,context);
context.fixture={state:'idle',operation:null,message:'idle',conflicts:[],
  profile:{mode:'1280x720@60',exposure_dynamic_framerate:0,duration_seconds:0},
  profile_locked:false,profile_durations:[0,10,30,300,600,1800,3600],
  profile_options:[{mode:'1280x720@60',supported:true,reasons:[]},
                   {mode:'800x600@30',supported:true,reasons:[]},
                   {mode:'1920x1080@60',supported:false,reasons:['right: unsupported']}],
  profile_support:{state:'fake',fake:true},preview:{cameras:{}},
  recording:{active:false,session:null,elapsed_s:null,exit_code:null,cameras:{}},
  disk:{free_mib:10000},limits:{min_free_mib:4096,critical_free_mib:1024,preview_publish_fps:8}};
vm.runInContext("token='test-token';lastStatus=fixture;render();",context);
(async()=>{
  if(input.case==='draft') {
    assert.equal(nodes['profile-mode'].value,'1280x720@60');
    assert.equal(nodes['profile-apply'].disabled,true);
    assert.equal(nodes['record-start'].disabled,false);
    assert.equal(nodes['profile-mode'].children[2].disabled,true);
    nodes['profile-mode'].value='800x600@30';
    nodes['profile-dynamic'].value='1';
    nodes['profile-duration'].value='30';
    nodes['profile-mode'].listeners.change();
    vm.runInContext('render();render();',context);
    assert.equal(nodes['profile-mode'].value,'800x600@30');
    assert.equal(nodes['profile-dynamic'].value,'1');
    assert.equal(nodes['profile-duration'].value,'30');
    assert.equal(nodes['profile-apply'].disabled,false);
    assert.equal(nodes['record-start'].disabled,true);
    await nodes['profile-form'].listeners.submit({preventDefault:()=>{}});
    const sent=requests.find(x=>x.path==='/api/profile/apply');
    assert.ok(sent);assert.equal(sent.options.headers['X-USV-Token'],'test-token');
    assert.deepEqual(JSON.parse(sent.options.body),{
      mode:'800x600@30',exposure_dynamic_framerate:1,duration_seconds:30});
  } else {
    for(const state of ['preview','starting','recording','stopping','error']) {
      context.fixture.state=state;context.fixture.profile_locked=true;
      vm.runInContext('lastStatus=fixture;render();',context);
      assert.equal(nodes['profile-mode'].disabled,true);
      assert.equal(nodes['profile-apply'].disabled,true);
      assert.equal(nodes['profile-check'].disabled,true);
    }
    context.fixture.state='idle';context.fixture.profile_locked=false;
    context.fixture.profile_support.state='checking';
    vm.runInContext('lastStatus=fixture;render();',context);
    assert.equal(nodes['record-start'].disabled,true);
    assert.equal(nodes['preview-start'].disabled,true);
    context.fixture.profile_support.state='failed';
    context.fixture.profile_support.error='probe failed';
    vm.runInContext('render();',context);
    assert.ok(nodes['profile-support'].textContent.includes('probe failed'));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""

@unittest.skipUnless(shutil.which("node"),"Node.js отсутствует: JS/DOM-заглушка пропущена")
class PageJavascriptTests(unittest.TestCase):
    def run_case(self,name):
        script=PANEL_HTML.split('<script>',1)[1].split('</script>',1)[0]
        script=script.replace('__CAMERAS_JSON__','[]')
        result=subprocess.run([shutil.which('node'),'-e',HARNESS],
            input=json.dumps({'script':script,'case':name}),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_draft_survives_poll_and_apply_sends_numeric_fields(self):
        self.run_case('draft')

    def test_busy_states_lock_fields_and_probe_failure_disables_start(self):
        self.run_case('locked')

if __name__=='__main__':unittest.main()
