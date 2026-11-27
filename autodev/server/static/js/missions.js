import { $, api, esc, toast, getActiveProjectId, root } from "./api.js";
import { mountMissionExecution } from "./mission-execution.js";

const labels = {NEEDS_ASSESSMENT:"待评估需求",WAITING_ANSWERS:"请确认以下关键问题",WAITING_PREREQUISITES:"先完成交付前置条件",NEEDS_CHECKLIST:"待检查规格质量",CHECKLIST_BLOCKED:"规格质量检查未通过，请先修订规格",WAITING_APPROVAL:"规格已准备好，请审阅批准",NEEDS_PLAN:"需求已批准，可继续形成实施方案",READY_TO_PUBLISH:"工作包已准备好",PUBLISHED:"已纳入研发计划",RUNNING:"正在处理需求"};
const intents = {new_system:"全新系统",feature:"增量特性",bugfix:"缺陷修复",refactor:"重构加固",documentation:"逆向文档"};
let project = "", selected = "", draft = "", intent = "feature", generation = 0, evidence = null;
const drafts = new Map(), evidenceDrafts = new Map();
let lastDiff=null;
Object.assign(labels, {NEEDS_REWORK:"检查要求修正，可自动处理", REWORK_STOPPED:"自动修正已暂停", REVIEW_BLOCKED:"检查条件尚未满足"});

export function codepointLength(text) {
  return Array.from(String(text ?? "")).length;
}

export function renderMissions(data, refresh) {
  if (!Array.isArray(data.missions)) return;
  const pid = getActiveProjectId(), ticket = ++generation;
  if (pid !== project) { project=pid; selected=""; draft=""; intent="feature"; evidence=null; drafts.clear(); evidenceDrafts.clear(); lastDiff=null; }
  const active=()=>getActiveProjectId()===pid && generation===ticket;
  const docs=$("docs-center-mount");
  let mount=$("missions-mount");
  if (!mount) { mount=document.createElement("section"); mount.id="missions-mount"; docs.before(mount); }
  let advanced=$("missions-advanced");
  if (!advanced) { advanced=document.createElement("details");advanced.id="missions-advanced";advanced.innerHTML="<summary>高级：功能规格、文档修订与单步执行</summary>";docs.before(advanced); }
  const old=$("specifications-mount");if(old)advanced.append(old);
  const council=$("view-council")?.querySelector(".council-layout");if(council)advanced.append(council);
  const legacyPlan=$("btn-plan-extra")?.closest(".inline-form");if(legacyPlan)legacyPlan.hidden=Boolean(data.enabled||data.missions.length);
  const legacyDebate=$("btn-run-council");if(legacyDebate)legacyDebate.hidden=Boolean(data.enabled||data.missions.length);
  const interject=$("btn-council-interject")?.closest(".interject-box");if(interject)interject.hidden=Boolean(data.enabled||data.missions.length);
  if (!selected && data.missions.length) selected=data.missions[0].feature_id;
  const mission=data.missions.find(m=>m.feature_id===selected);
  const feature=mission?.feature_id || "feature-"+Date.now().toString(36);
  const run=data.running || mission?.state==="RUNNING";
  const showAssessment=mission?.route && (mission.state_before_running||mission.state)!=="NEEDS_ASSESSMENT";
  const blocking=(mission?.questions||[]).filter(q=>q.status==="OPEN" && q.severity==="BLOCKING");
  const defaults=(mission?.questions||[]).filter(q=>q.status==="AUTO_DEFAULTED");
  const featObj=data.features.find(f=>f.feature_id===feature);
  const source=featObj?.sources?.spec;
  const qr=mission?.quality_review || featObj?.quality_review || null;
  const renderFinding=f=>`<li><strong>[${esc(f.severity)}] ${esc(f.id||"")}</strong>${f.category?` (${esc(f.category)})`:""}：${esc(f.message)}${f.target?` <span class="muted">位置：${esc(f.target)}</span>`:""}</li>`;
  const renderQualityReview=()=>{
    if(!qr || (qr.status==="MISSING" && !(qr.history||[]).length))return "";
    const c=qr.counts||{CRITICAL:0,WARNING:0,INFO:0,total:0};
    const tag=qr.status==="STALE"?"（已过期）":qr.status==="MISSING_METADATA"?"（缺少发现记录）":"";
    const summary=`质量检查结果${qr.checklist_rev_no?` · 第 ${qr.checklist_rev_no} 版`:""}${tag} · 严重 ${c.CRITICAL||0} / 提醒 ${c.WARNING||0} / 信息 ${c.INFO||0}`;
    const currentList=(qr.findings||[]).length?`<ul>${qr.findings.map(renderFinding).join("")}</ul>`:`<p class="muted">${qr.metadata_present?"当前版本无未结质量发现。":"缺少结构化发现记录，不能视为无问题。"}</p>`;
    const hist=(qr.history||[]).length?`<details class="quality-history"><summary>历史检查记录（${qr.history.length} 版）</summary>${qr.history.map(h=>`<div><p class="muted">第 ${h.rev_no} 版 · 严重 ${h.counts?.CRITICAL||0} / 提醒 ${h.counts?.WARNING||0} / 信息 ${h.counts?.INFO||0}</p>${(h.findings||[]).length?`<ul>${h.findings.map(renderFinding).join("")}</ul>`:`<p class="muted">无记录</p>`}</div>`).join("")}</details>`:"";
    return `<details id="mission-quality-review"><summary>${esc(summary)}</summary><p class="muted">${esc(qr.next_action||"")}</p><div class="quality-current"><strong>当前版本发现</strong>${currentList}</div>${hist}</details>`;
  };
  const field=q=>`<div class="mission-question"><label for="mission-answer-${q.clarify_id}">${esc(q.question)}</label><p class="muted">${esc(q.rationale)}</p>${q.options.length?`<select id="mission-answer-${q.clarify_id}" data-question="${q.clarify_id}"><option value="">请选择</option>${q.options.map(o=>`<option value="${esc(o)}" ${o===(drafts.get(q.clarify_id)?.answer||q.answer)?"selected":""}>${esc(o)}${o===q.default_option?"（有依据的推荐项）":""}</option>`).join("")}</select>`:`<input id="mission-answer-${q.clarify_id}" data-question="${q.clarify_id}" value="${esc(drafts.get(q.clarify_id)?.answer||q.answer||"")}">`}<label class="muted" for="mission-supplement-${q.clarify_id}">补充说明（可选）</label><textarea id="mission-supplement-${q.clarify_id}" data-supplement="${q.clarify_id}" rows="2">${esc(drafts.get(q.clarify_id)?.supplement||"")}</textarea>${q.status==="AUTO_DEFAULTED"?`<button class="btn btn-sm" data-default-save="${q.clarify_id}">更新此默认值</button>`:""}</div>`;
  mount.className="mission-panel";
  mount.innerHTML=`<header class="mission-head"><div><h2>需求与交付</h2><p class="muted">说明目标，确认必要决策，按批准的规格交付。</p></div><button id="mission-refresh" class="btn btn-sm">刷新进度</button></header>
    <div class="mission-toolbar"><label>当前需求 <select id="mission-select"><option value="new">新增需求</option>${data.missions.map(m=>`<option value="${esc(m.feature_id)}" ${m.feature_id===selected?"selected":""}>${esc(m.brief.slice(0,42))}</option>`).join("")}</select></label><button class="btn btn-sm" id="mission-preflight">检查执行条件</button></div>
    ${!mission?`<label for="mission-intent">任务类型</label><select id="mission-intent">${Object.entries(intents).map(([k,v])=>`<option value="${k}" ${k===intent?"selected":""}>${v}</option>`).join("")}</select><label for="mission-brief">这次要完成什么</label><textarea id="mission-brief" rows="7" maxlength="50000" placeholder="说明目标、首版范围和验收方式。已有文档可在建项时附带。">${esc(draft)}</textarea><p class="muted">项目来源和执行池沿用当前项目设置，保存后再检查执行条件。</p><button class="btn btn-primary" id="mission-save">保存并评估需求</button>`:
    `<div class="mission-status" role="status">${esc(labels[mission.state]||mission.state)}${mission.spec_rev_no?` · 规格第 ${mission.spec_rev_no} 版`:""}</div><section id="mission-execution"></section><p>${esc(mission.brief)}</p>
    ${showAssessment?`<p class="muted">${{greenfield:"全新项目",existing_local:"已有本机代码",existing_git:"已有 Git 仓库"}[mission.route.source_origin]} · ${mission.route.clarity_level==="READY_LITE"?"局部轻量交付":mission.route.clarity_level==="NEEDS_CLARIFICATION"?"有待确认的交付边界":"标准交付"} · 风险${{low:"低",medium:"中",high:"高"}[mission.assessment.risk]}</p><p>${esc(mission.assessment.rationale)}</p>${mission.route.limitation?`<p class="muted">${esc(mission.route.limitation)}</p>`:""}`:""}
    ${showAssessment && mission.config_alignment && !mission.config_alignment.aligned?`<div class="mission-config-mismatch" id="mission-config-alignment"><p class="muted">${esc(mission.config_alignment.mismatch_reason||"本次评估推荐配置与当前锁定配置不同。")}</p>${mission.config_alignment.can_convert_to_recommended?`<button class="btn btn-sm" id="mission-convert-profile" ${run?"disabled":""}>显式切换至推荐配置（${esc(mission.config_alignment.recommended_preset)} / ${esc(mission.config_alignment.recommended_process)}）</button>`:""}</div>`:(showAssessment && mission.actual_process && mission.actual_process!==mission.route.process_template?`<p class="muted">本次评估推荐的流程与当前项目不同。项目继续保留已锁定的审批流程，建议不会自动覆盖既有设置。</p>`:"")}
    ${mission.last_error||mission.blocker?`<p role="alert">${esc(mission.last_error||mission.blocker)}</p>`:""}
    ${blocking.map(field).join("")}${blocking.length?`<button class="btn btn-primary" id="mission-answers" ${run?"disabled":""}>确认回答并继续</button>`:""}
    ${defaults.length?`<details><summary>已采用 ${defaults.length} 项低风险默认值，可修改</summary>${defaults.map(field).join("")}</details>`:""}
    ${(mission.prerequisites||[]).map(p=>`<div class="mission-question"><strong>${esc(p.description)}</strong><p>${esc(p.evidence_needed)}</p><label>负责人核验依据<input data-prerequisite="${esc(p.key)}" placeholder="写明实际检查结果或证据位置"></label><button class="btn btn-sm" data-evidence-save="${esc(p.key)}">记录核验依据</button></div>`).join("")}
    ${source?`<details><summary>审阅当前规格 · 第 ${source.rev_no} 版</summary><pre class="mission-document">${esc(source.content_text)}</pre></details>`:""}
    ${renderQualityReview()}
    ${(mission.repair?.findings || []).length ? `<div class="mission-question"><strong>当前待处理问题</strong><p>已自动修正 ${Number(mission.repair.round || 0)} 轮</p><ul>${mission.repair.findings.map(f => `<li>${esc(f.message)}${f.repair_target ? `（${esc({plan:"方案修正",tasks:"任务修正",spec:"规格决策"}[f.repair_target] || f.repair_target)}）` : ""}</li>`).join("")}</ul></div>` : ""}
    ${["NEEDS_ASSESSMENT","NEEDS_CHECKLIST","NEEDS_PLAN","NEEDS_REWORK"].includes(mission.state)?`<button class="btn btn-primary" id="mission-continue" ${run?"disabled":""}>${mission.state==="NEEDS_REWORK"?"修正并复审":mission.last_error?"从已保存步骤继续":"继续需求交付"}</button>`:""}
    ${mission.state==="WAITING_APPROVAL"?`<p>请到工作台的需求审批门审阅当前版本。批准后点击继续，自动生成方案、任务和一致性检查。</p><button class="btn" id="mission-home">打开工作台审批</button>`:""}
    ${mission.state==="READY_TO_PUBLISH"?`${mission.packages.map(p=>`<div class="mission-question"><strong>${esc(p.task.title)}</strong><p>${esc(p.task.acceptance_criteria.join("；"))}</p><p class="muted">修改范围：${esc(p.task.allowed_paths.join("、"))}</p></div>`).join("")}<button class="btn btn-primary" id="mission-publish">发布以上工作包</button>`:""}
    ${mission.state==="PUBLISHED"?`<p>规格和工作包已冻结。请在工作台核对实施方案并启动研发，交付结果由现有验证和合入流程记录。</p><button class="btn" id="mission-home">打开研发工作台</button>`:""}
    ${mission.state==="RUNNING"&&!data.running?`<p>上次流程已中断。恢复前将检查原生执行是否结束。</p><button class="btn" id="mission-recover">核对执行记录并恢复</button>`:""}`}
    <div id="mission-checks" aria-live="polite"></div><div id="mission-answer-revision" aria-live="polite"></div>`;
  if (mission) mountMissionExecution(feature, refresh);
  const showChecks=value=>{if(!active())return;$("mission-checks").innerHTML=`<p><strong>${value.ready?"派发前配置检查通过":"执行条件尚未满足，需求已保留"}</strong></p><ul>${value.checks.map(c=>`<li>${c.ok?"已确认":"待处理"}：${esc(c.message)}</li>`).join("")}</ul><p class="muted">${esc(value.verification)}</p>`;};
  if(evidence)showChecks(evidence);
  const showDiff=()=>{if(lastDiff?.feature===feature)$("mission-answer-revision").innerHTML=`<details open><summary>回答已保存，以下为本次变更</summary><pre class="mission-document">${esc(lastDiff.diff)}</pre></details>`;};showDiff();
  mount.querySelectorAll("[data-prerequisite]").forEach(el=>{el.value=evidenceDrafts.get(feature+":"+el.dataset.prerequisite)||"";el.addEventListener("input",()=>evidenceDrafts.set(feature+":"+el.dataset.prerequisite,el.value));});
  const action=fn=>async ev=>{
    const button=ev.currentTarget;
    if(!active()||!button||button.disabled)return;
    button.disabled=true;
    try{await fn();}catch(err){if(active())toast(err.message);}
    finally{if(active())button.disabled=false;}
  };
  const proceed=async()=>{const result=await api(`/vernal-api/missions/${encodeURIComponent(feature)}/continue`,{method:"POST",body:JSON.stringify({expected_revision:mission.revision})});if(!active())return;evidence=result.preflight;await refresh();};
  $("mission-select").addEventListener("change",ev=>{selected=ev.target.value;drafts.clear();evidence=null;refresh();});
  $("mission-refresh").addEventListener("click",()=>refresh());
  $("mission-preflight").addEventListener("click",action(async()=>{const value=await api("/vernal-api/missions/preflight");if(active()){evidence=value;showChecks(value);}}));
  $("mission-brief")?.addEventListener("input",ev=>{draft=ev.target.value;});
  $("mission-intent")?.addEventListener("change",ev=>{intent=ev.target.value;});
  $("mission-save")?.addEventListener("click",action(async()=>{
    const saved=await api("/vernal-api/missions",{method:"POST",body:JSON.stringify({feature_id:feature,brief:$("mission-brief").value,intent:$("mission-intent").value,expected_revision:0})});
    if(!active())return;selected=saved.feature_id;draft="";
    const result=await api(`/vernal-api/missions/${encodeURIComponent(feature)}/continue`,{method:"POST",body:JSON.stringify({expected_revision:saved.revision})});if(active()){evidence=result.preflight;await refresh();}
  }));
  $("mission-continue")?.addEventListener("click",action(proceed));
  mount.querySelectorAll("[data-question],[data-supplement]").forEach(el=>el.addEventListener("input",()=>{const id=Number(el.dataset.question||el.dataset.supplement);drafts.set(id,{answer:$("mission-answer-"+id).value,supplement:$("mission-supplement-"+id).value});}));
  const submit=async questions=>{
    const answers=questions.map(q=>{
      const answer=$("mission-answer-"+q.clarify_id).value;
      const supplement=$("mission-supplement-"+q.clarify_id).value;
      if(!answer.trim())throw new Error("请先回答："+q.question);
      if(codepointLength(answer)>10000||codepointLength(supplement)>10000)throw new Error("回答和补充说明各不能超过 10000 字");
      return {clarify_id:q.clarify_id,answer,supplement};
    });
    const result=await api(`/vernal-api/missions/${encodeURIComponent(feature)}/answers`,{method:"POST",body:JSON.stringify({expected_rev_id:mission.spec_rev_id,answers})});
    if(!active())return;evidence=result.preflight||null;drafts.clear();lastDiff={feature,revision:result.spec_rev_id,diff:result.diff};if(result.continuation_error)toast("回答已保存，后续评估尚未启动："+result.continuation_error);await refresh();
    if(getActiveProjectId()===pid && selected===feature)showDiff();
  };
  $("mission-answers")?.addEventListener("click",action(()=>submit(blocking)));
  mount.querySelectorAll("[data-default-save]").forEach(btn=>btn.addEventListener("click",action(()=>submit(defaults.filter(q=>q.clarify_id===Number(btn.dataset.defaultSave))))));
  mount.querySelectorAll("[data-evidence-save]").forEach(btn=>btn.addEventListener("click",action(async()=>{await api(`/vernal-api/missions/${encodeURIComponent(feature)}/prerequisites`,{method:"POST",body:JSON.stringify({expected_revision:mission.revision,key:btn.dataset.evidenceSave,evidence:mount.querySelector(`[data-prerequisite="${btn.dataset.evidenceSave}"]`).value})});if(active())await refresh();})));
  $("mission-publish")?.addEventListener("click",action(async()=>{await api(`/vernal-api/specifications/${encodeURIComponent(feature)}/publish`,{method:"POST",body:JSON.stringify({expected_hashes:mission.packages.map(p=>p.compiled_task_hash)})});if(active())await refresh();}));
  $("mission-convert-profile")?.addEventListener("click",action(async()=>{
    const ca=mission.config_alignment;
    if(!ca?.can_convert_to_recommended)return;
    await api(`/vernal-api/missions/${encodeURIComponent(feature)}/convert-profile`,{method:"POST",body:JSON.stringify({expected_revision:mission.revision,target_preset:ca.recommended_preset,target_process:ca.recommended_process,reason:"负责人确认切换至当前推荐轻量配置"})});
    if(active())await refresh();
  }));
  $("mission-recover")?.addEventListener("click",action(async()=>{await api("/vernal-api/missions/recover",{method:"POST",body:"{}"});if(active())await refresh();}));
  $("mission-home")?.addEventListener("click",()=>root().querySelector('[data-view="overview"]')?.click());
}
