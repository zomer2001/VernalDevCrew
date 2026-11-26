import { $, api, esc, toast, getActiveProjectId as currentProjectId } from "./api.js";
import { renderMissions } from "./missions.js";

const steps = {constitution:"建立项目约束",specify:"整理功能规格",clarify:"澄清需求边界",checklist:"检查规格质量",plan:"制定技术方案",tasks:"拆解实施任务",analyze:"核对交付一致性"};
const kinds = {constitution:"项目宪章",spec:"功能规格",plan:"技术方案",tasks:"任务草案",checklist:"质量检查",analyze:"一致性检查"};
let selected = "", project = "", generation = 0;

export async function renderSpecifications() {
  const docs = $("docs-center-mount");
  if (!docs) return;
  let mount = $("specifications-mount");
  if (!mount) { mount=document.createElement("section"); mount.id="specifications-mount"; docs.before(mount); }
  const ticket = ++generation, pid = currentProjectId();
  if (project !== pid) { selected=""; project=pid; }
  const active = () => ticket === generation && pid === currentProjectId();
  let data;
  try { data=await api("/vernal-api/specifications"); }
  catch (err) { if (active()) mount.textContent=`功能规格暂不可用：${err.message}`; return; }
  if (!active()) return;
  renderMissions(data,renderSpecifications);
  const act = fn => async ev => {
    if (!active()) return;
    const button=ev.currentTarget; button.disabled=true;
    try { await fn(); if (active()) await renderSpecifications(); }
    catch(err) { if (active()) toast(err.message); }
    finally { button.disabled=false; }
  };
  if (!data.enabled) {
    mount.innerHTML=`<div class="card"><h3>功能规格与任务交付</h3><p>用项目宪章、功能规格和验收条件约束研发。启用后，需求汇总页从功能规格生成。</p>
      <label>规格预设 <select id="spec-preset"><option value="vernal-core">标准项目</option><option value="vernal-lite">轻量项目</option></select></label>
      <button class="btn btn-primary" id="spec-enable">${data.legacy ? "保留旧规格并迁移" : "启用功能规格"}</button></div>`;
    $("spec-enable").addEventListener("click",act(async()=>{ await api("/vernal-api/specifications/enable",{method:"POST",body:JSON.stringify({preset_id:$("spec-preset").value,migrate:data.legacy})}); }));
    return;
  }
  if (!selected) selected=data.features[0]?.feature_id||"001-feature";
  const feature=data.features.find(f=>f.feature_id===selected);
  const sources={constitution:data.constitution,...(feature?.sources||{})};
  const questions=(feature?.clarifications||[]).filter(q=>["OPEN","AUTO_DEFAULTED"].includes(q.status));
  const handoff=feature?.last_step?.handoff_brief;
  mount.innerHTML=`<div class="card"><div class="card-head"><div><h3>功能规格与任务交付</h3><p class="card-desc">先确认项目约束与需求，再批准规格、规划任务和检查一致性。任务发布后在原有工作台审批并启动。</p></div><button class="btn btn-sm" id="spec-refresh">刷新状态</button></div>
    <div class="inline-form"><label>功能编号 <input id="spec-feature" list="spec-features" value="${esc(selected)}" pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,79}"></label><datalist id="spec-features">${data.features.map(f=>`<option value="${esc(f.feature_id)}"></option>`).join("")}</datalist><button id="spec-select" class="btn">查看或新增功能</button><span>${data.running?"当前步骤执行中，完成后刷新查看":"等待选择下一步骤"}</span></div>
    <label for="spec-instructions">本次目标、约束或补充资料</label><textarea id="spec-instructions" rows="3" style="width:100%" placeholder="例如：新增项目成员邀请功能，本轮只支持管理员邀请。"></textarea>
    <div class="btn-group" style="flex-wrap:wrap">${Object.entries(steps).map(([key,label])=>`<button class="btn btn-sm" data-spec-step="${key}" ${data.running?"disabled":""}>${label}</button>`).join("")}</div>
    <p class="muted">需求审批使用现有流程门禁。关键澄清未回答时，规划和发布会被阻止。</p>
    ${questions.map(q=>`<div class="card"><strong>${esc(q.question)}</strong><p>${q.severity==="BLOCKING"?"需要负责人确认":"已采用推荐默认值，可修改"} ${esc(q.rationale)}</p><label>回答 ${q.options.length?`<select data-answer="${q.clarify_id}">${q.options.map(o=>`<option ${o===q.answer?"selected":""}>${esc(o)}</option>`).join("")}</select>`:`<input data-answer="${q.clarify_id}" value="${esc(q.answer||"")}">`}</label><button class="btn btn-sm" data-submit-answer="${q.clarify_id}">确认回答</button></div>`).join("")}
    <label>查看与修订 <select id="spec-source">${Object.entries(kinds).map(([key,label])=>`<option value="${key}">${label}${sources[key]?` · 第 ${sources[key].rev_no} 版`:" · 尚未生成"}</option>`).join("")}</select></label>
    <textarea id="spec-content" rows="10" style="width:100%;font-family:var(--mono)"></textarea><button id="spec-save" class="btn btn-sm">保存新修订</button><button id="spec-restore" class="btn btn-sm" title="备份外部修改，并用已保存修订恢复磁盘文件">备份并恢复文件副本</button>
    ${handoff?`<div class="cmsg-grid">${Object.entries({delivered:"本次交付",integration:"接入方式",boundaries:"边界与限制",verification:"验证结果"}).map(([key,label])=>`<div class="cmsg-box"><h5>${label}</h5><p>${esc(handoff[key])}</p></div>`).join("")}</div>`:""}
    <div class="inline-form"><button id="spec-compile" class="btn btn-primary">预览可执行工作包</button></div><div id="spec-packages"></div>
    <details><summary>版本诊断</summary><pre>${esc(JSON.stringify(data.assets,null,2))}</pre></details></div>`;
  $("spec-refresh").addEventListener("click",()=>renderSpecifications());
  $("spec-select").addEventListener("click",()=>{ const id=$("spec-feature").value.trim(); if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(id)){toast("请使用字母、数字和下划线填写功能编号");return;} selected=id;renderSpecifications(); });
  mount.querySelectorAll("[data-spec-step]").forEach(button=>button.addEventListener("click",act(async()=>{ await api("/vernal-api/specifications/run",{method:"POST",body:JSON.stringify({feature_id:selected,step:button.dataset.specStep,instructions:$("spec-instructions").value})});toast("已提交步骤，请在执行记录中查看进度"); })));
  const load = ()=>{ const kind=$("spec-source").value; $("spec-content").value=sources[kind]?.content_text||""; $("spec-content").readOnly=["analyze","checklist"].includes(kind); $("spec-save").disabled=$("spec-content").readOnly||data.running; };
  $("spec-source").value=feature?"spec":"constitution"; load(); $("spec-source").addEventListener("change",load);
  $("spec-restore").addEventListener("click",act(async()=>{const kind=$("spec-source").value;if(!sources[kind])throw Error("尚无可恢复修订");const result=await api(`/vernal-api/specifications/${encodeURIComponent(selected)}/${kind}/restore`,{method:"POST",body:JSON.stringify({expected_rev_id:sources[kind].rev_id})});toast(result.backup_path?`已恢复，外部修改保存在 ${result.backup_path}`:"已恢复已保存的版本");}));
  $("spec-save").addEventListener("click",act(async()=>{ const kind=$("spec-source").value; await api(`/vernal-api/specifications/${encodeURIComponent(selected)}/${kind}`,{method:"PUT",body:JSON.stringify({content:$("spec-content").value,expected_rev_id:sources[kind]?.rev_id||0})});toast("已保存新修订，相关审批按新版本重新核对"); }));
  mount.querySelectorAll("[data-submit-answer]").forEach(button=>button.addEventListener("click",act(async()=>{ const id=button.dataset.submitAnswer;await api(`/vernal-api/specifications/clarifications/${id}/answer`,{method:"POST",body:JSON.stringify({answer:mount.querySelector(`[data-answer="${id}"]`).value,expected_rev_id:feature.sources.spec.rev_id})}); })));
  $("spec-compile").addEventListener("click",async ev=>{
    const button=ev.currentTarget;button.disabled=true;
    try {
      const preview=await api(`/vernal-api/specifications/${encodeURIComponent(selected)}/compile`); if(!active())return;
      $("spec-packages").innerHTML=preview.packages.map(p=>`<div class="cmsg-box"><h4>${esc(p.task.title)}</h4><p>${esc(p.task.description)}</p><p>修改范围：${esc(p.task.allowed_paths.join("、"))}</p><p>验收：${esc(p.task.acceptance_criteria.join("；"))}</p><p>验证：${esc(p.task.verify_cmds.join("；"))}</p><p>合并 ${p.source_task_keys.length} 个实施步骤</p></div>`).join("")+`<button id="spec-publish" class="btn btn-primary">发布以上工作包</button>`;
      $("spec-publish").addEventListener("click",act(async()=>{const result=await api(`/vernal-api/specifications/${encodeURIComponent(selected)}/publish`,{method:"POST",body:JSON.stringify({expected_hashes:preview.packages.map(p=>p.compiled_task_hash)})});toast(`已发布 ${result.task_ids.length} 个工作包，请到工作台审批计划并启动`);}));
    } catch(err){if(active())toast(err.message);} finally{button.disabled=false;}
  });
}

export function initSpecificationsModule({registerViewHooks}) {
  registerViewHooks({onCouncilOpen:()=>renderSpecifications()});
}
