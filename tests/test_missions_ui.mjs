import assert from "node:assert/strict";
import test from "node:test";
import { setActiveProjectId } from "../autodev/server/static/js/api.js";
import { renderMissions } from "../autodev/server/static/js/missions.js";

function dom() {
  const elements=new Map();
  function element() { return {value:"",disabled:false,events:{},dataset:{},
    addEventListener(name,fn){this.events[name]=fn;},append(){},before(node){elements.set(node.id,node);},
    querySelectorAll(){return [];},querySelector(){return null;},
    set innerHTML(html){this.html=html;for(const m of html.matchAll(/id="([^"]+)"/g))elements.set(m[1],element());},
    get innerHTML(){return this.html;}}; }
  elements.set("docs-center-mount",element());elements.set("specifications-mount",element());
  globalThis.CSS={escape:String};
  globalThis.document={getElementById:id=>elements.get(id)||null,querySelector:s=>elements.get(s.slice(1))||null,createElement:element};
  return elements;
}

function state(questions=[]) { return {running:false,features:[{feature_id:"f1",sources:{spec:{rev_id:42,rev_no:3,content_text:"# 保存回答"}}}],missions:[{
  feature_id:"f1",revision:2,spec_rev_id:42,spec_rev_no:3,brief:'保存回答 <script>bad()</script>',state:questions.length?"WAITING_ANSWERS":"NEEDS_ASSESSMENT",questions,prerequisites:[],
}]}; }

test("clarification UI preserves project and revision, offers no implicit answer and posts one round",async()=>{
  const el=dom();setActiveProjectId("project-a");
  const data=state([{clarify_id:7,severity:"BLOCKING",status:"OPEN",question:'是否保留 <img src=x>',options:["是","否"],rationale:"涉及数据承诺"}]);
  let refreshed=0;renderMissions(data,async()=>{refreshed++;});
  const html=el.get("missions-mount").innerHTML;
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
  assert.match(html,/<option value="">请选择<\/option>/);
  assert.equal((html.match(/btn btn-primary/g)||[]).length,1);
  el.get("mission-answer-7").value="否";el.get("mission-supplement-7").value="只保留当前会话";
  let submitted;
  globalThis.fetch=async(url,opts)=>{submitted={url,opts,body:JSON.parse(opts.body)};return {ok:true,json:async()=>({saved:true,started:false,spec_rev_id:43,diff:'+ 确认：否 <script>bad()</script>'})};};
  const button=el.get("mission-answers");await button.events.click({currentTarget:button});
  assert.equal(submitted.opts.headers["X-Vernal-Project"],"project-a");
  assert.equal(submitted.body.expected_rev_id,42);
  assert.deepEqual(submitted.body.answers,[{clarify_id:7,answer:"否",supplement:"只保留当前会话"}]);
  assert.equal(refreshed,1);
  assert.match(el.get("mission-answer-revision").innerHTML,/新修订 43/);
  assert.doesNotMatch(el.get("mission-answer-revision").innerHTML,/<script>/);
});

test("zero unresolved questions leads directly to continuation and late responses never enter another project",async()=>{
  const el=dom();setActiveProjectId("project-b");renderMissions(state(),async()=>{});
  assert.doesNotMatch(el.get("missions-mount").innerHTML,/id="mission-answers"/);
  assert.match(el.get("missions-mount").innerHTML,/id="mission-continue"/);
  let resolve;
  globalThis.fetch=async()=>new Promise(r=>{resolve=r;});
  const btn=el.get("mission-preflight");const pending=btn.events.click({currentTarget:btn});
  setActiveProjectId("project-c");renderMissions({missions:[],features:[]},async()=>{});
  resolve({ok:true,json:async()=>({ready:false,checks:[{ok:false,message:"旧项目错误"}],verification:"旧记录"})});await pending;
  assert.doesNotMatch(el.get("missions-mount").innerHTML,/旧项目错误/);
  assert.ok(!el.get("mission-checks").innerHTML?.includes("旧项目错误"));
});

test("low risk defaults are collapsed and do not render a blocking question button",()=>{
  const el=dom();setActiveProjectId("project-d");
  const data=state([{clarify_id:8,severity:"DEFAULTED",status:"AUTO_DEFAULTED",question:"分页大小",options:["20","50"],answer:"20",default_option:"20",rationale:"使用现有页面默认值"}]);
  data.missions[0].state="WAITING_APPROVAL";
  renderMissions(data,async()=>{});
  const html=el.get("missions-mount").innerHTML;
  assert.match(html,/<details><summary>已采用 1 项低风险默认值/);
  assert.doesNotMatch(html,/id="mission-answers"/);
  assert.match(html,/打开工作台审批/);
});
