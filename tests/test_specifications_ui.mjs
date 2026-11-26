import assert from "node:assert/strict";
import test from "node:test";
import {setActiveProjectId} from "../autodev/server/static/js/api.js";
import {renderSpecifications} from "../autodev/server/static/js/specifications.js";

test("specification panel drops stale project responses and binds edits to the displayed revision",async()=>{
  const elements=new Map();
  function element(){return {value:"",events:{},classList:{add(){},remove(){}},
    addEventListener(name,fn){this.events[name]=fn;},querySelectorAll(){return [];},
    set innerHTML(html){this.html=html;for(const match of html.matchAll(/id="([^"]+)"/g))elements.set(match[1],element());},
    get innerHTML(){return this.html;}};}
  elements.set("docs-center-mount",{before(host){elements.set(host.id,host);}});
  globalThis.CSS={escape:String};
  globalThis.document={getElementById:id=>elements.get(id)||null,querySelector:s=>elements.get(s.slice(1))||null,createElement:element};
  let release;
  globalThis.fetch=async()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({enabled:false,legacy:true,features:[]})});});
  setActiveProjectId("old");const old=renderSpecifications();
  setActiveProjectId("current");
  let request;
  globalThis.fetch=async(path,opts)=>{
    request={path,opts};
    return {ok:true,json:async()=>({enabled:true,assets:{preset_id:"vernal-core"},features:[{feature_id:"001-feature",sources:{spec:{rev_id:42,rev_no:7,content_text:"# 当前规格"}},clarifications:[{clarify_id:1,status:"OPEN",severity:"BLOCKING",question:'<img src=x onerror="bad()">',options:[]}]}]})};
  };
  await renderSpecifications();
  release();await old;
  const html=elements.get("specifications-mount").innerHTML;
  assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);assert.match(html,/预览可执行工作包/);
  assert.equal(elements.get("spec-content").value,"# 当前规格");
  let saved;
  globalThis.fetch=async(path,opts)=>{
    if(opts.method==="PUT")saved={path,body:JSON.parse(opts.body),project:opts.headers["X-Vernal-Project"]};
    return {ok:true,json:async()=>({enabled:false,features:[]})};
  };
  elements.get("spec-content").value="# 修改后的规格";
  const button=elements.get("spec-save");await button.events.click({currentTarget:button});
  assert.equal(saved.body.expected_rev_id,42);assert.equal(saved.body.content,"# 修改后的规格");assert.equal(saved.project,"current");
});
