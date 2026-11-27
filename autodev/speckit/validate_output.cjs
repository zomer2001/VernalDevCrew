const fs = require("fs");
const path = require("path");
// Match Python's Unicode whitespace rules, including NEL and excluding BOM.
const SPACE = "[\\t-\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const strip = value => value.replace(new RegExp("^" + SPACE + "+|" + SPACE + "+$", "gu"), "");
const schemaPattern = pattern => pattern.replace(/\\s/g, SPACE).replace(/\\S/g, "[^" + SPACE.slice(1));
function validate(value, schema, where = "result") {
  if (schema.anyOf) {
    for (const choice of schema.anyOf) {
      try { validate(value, choice, where); return; } catch (e) { if (!(e instanceof Error)) throw e; }
    }
    throw Error(`${where}: 不满足任一允许类型`);
  }
  const checks = {object: () => value !== null && typeof value === "object" && !Array.isArray(value),
    array: () => Array.isArray(value), string: () => typeof value === "string", integer: () => Number.isInteger(value),
    number: () => typeof value === "number", boolean: () => typeof value === "boolean", null: () => value === null};
  if (checks[schema.type] && !checks[schema.type]()) throw Error(`${where}: 需要 ${schema.type}`);
  if (schema.enum && !schema.enum.includes(value)) throw Error(`${where}: 不属于允许值`);
  if ("const" in schema && value !== schema.const) throw Error(`${where}: 固定值不匹配`);
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const props = schema.properties || {};
    const missing = (schema.required || []).filter(name => !Object.hasOwn(value, name)).sort();
    if (missing.length) throw Error(`${where}: 缺少字段 [${missing.map(n => "'" + n + "'").join(", ")}]`);
    if (schema.additionalProperties === false && Object.keys(value).some(n => !Object.hasOwn(props, n))) throw Error(`${where}: 存在未声明字段`);
    for (const [name, item] of Object.entries(value)) {
      if (!(name in props) && schema.additionalProperties === false) throw Error(`${where}: 未声明字段 ${name}`);
      if (name in props) validate(item, props[name], `${where}.${name}`);
    }
  }
  if (Array.isArray(value)) {
    if (value.length < (schema.minItems || 0) || ("maxItems" in schema && value.length > schema.maxItems)) throw Error(`${where}: 数组长度无效`);
    value.forEach((item, i) => {
      const label = item && typeof item === "object" && !Array.isArray(item)
        ? ("package_key" in item ? " 工作包 " + item.package_key : "task_key" in item ? " 步骤 " + item.task_key : "") : "";
      validate(item, schema.items || {}, `${where}[${i}]${label}`);
    });
  }
  if (typeof value === "string" && ([...value].length < (schema.minLength || 0) || ("maxLength" in schema && [...value].length > schema.maxLength) ||
      (schema.pattern && !new RegExp(schemaPattern(schema.pattern), "u").test(value)))) throw Error(`${where}: 文本格式无效`);
}
function canonicalJson(value) {
  const sort = v => Array.isArray(v) ? v.map(sort) : v && typeof v === "object"
    ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sort(v[k])])) : v;
  return JSON.stringify(sort(value), null, 2) + "\n";
}
function overlap(a, b) {
  a = a.replace(/\/+$/, ""); b = b.replace(/\/+$/, "");
  return a === b || a.startsWith(b + "/") || b.startsWith(a + "/");
}
function commandValid(command) {
  if (typeof command !== "string" || !strip(command)) return false;
  let quote = "", escaped = false, token = false;
  for (const char of command) {
    if (escaped) { escaped = false; token = true; }
    else if (char === "\\" && quote !== "'") escaped = true;
    else if (quote) { if (char === quote) quote = ""; else token = true; }
    else if (char === "'" || char === '"') quote = char;
    else if (!new RegExp(SPACE, "u").test(char)) token = true;
  }
  return !quote && !escaped && token;
}
function ordered(dependencies, label) {
  const pending = new Map([...dependencies].map(([key, refs]) => [key, new Set(refs)]));
  const result = [];
  while (pending.size) {
    const ready = [...pending.keys()].filter(k => !pending.get(k).size).sort();
    if (!ready.length) throw Error(label + "依赖形成循环：" + [...pending.keys()].sort().join(", "));
    result.push(...ready);
    for (const key of ready) pending.delete(key);
    for (const refs of pending.values()) for (const key of ready) refs.delete(key);
  }
  return result;
}
function safeArtifactPath(root, relative) {
  if (typeof relative !== "string" || !relative || relative.startsWith("/") || (relative.includes("\\") || relative.includes("\0"))
      || /^[A-Za-z]:/.test(relative) || relative.split("/").some(p => ["", ".", "..", ".git", ".autodev"].includes(p)))
    throw Error("产物路径越界：" + String(relative));
  let cursor = root;
  for (const part of relative.split("/")) {
    cursor = path.join(cursor, part);
    try { if (fs.lstatSync(cursor).isSymbolicLink()) throw Error("产物路径包含符号链接：" + relative); }
    catch (e) { if (e.code !== "ENOENT") throw e; }
  }
  return cursor;
}
function validateWorkPackages(packages, schema, root = null) {
  if (!Array.isArray(packages) || !packages.length) throw Error("tasks 必须返回非空 work_packages");
  const errors = [], keys = new Set(), steps = new Map(), owners = new Map(), covered = new Set(), territories = [];
  const dependencies = new Map(), reqIds = new Set(schema["x-vernal-req-ids"] || []);
  for (const p of packages) {
    const key = p.package_key;
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$(?![\s\S])/.test(key) || keys.has(key)) errors.push(`工作包 ${key} 编号无效或重复`);
    keys.add(key); dependencies.set(key, new Set(p.depends_on_packages));
    for (const field of ["milestone_id", "squad", "module"])
      if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$(?![\s\S])/.test(p[field])) errors.push(`工作包 ${key} 的 ${field} 必须是安全的非空标识`);
    if (!strip(p.title)) errors.push(`工作包 ${key} 标题不能为空`);
    for (const field of ["acceptance_criteria", "allowed_paths", "verify_cmds"])
      if (!p[field].length || p[field].some(v => !strip(v))) errors.push(`工作包 ${key} 的 ${field} 不能为空`);
    for (const cmd of p.verify_cmds) if (!commandValid(cmd)) errors.push(`工作包 ${key} 验证命令为空或引号未闭合：${cmd}`);
    for (const raw of p.allowed_paths) {
      const value = raw.replace(/\/+$/, "");
      if (!value || value.startsWith("/") || /^[A-Za-z]:/.test(value) || (value.includes("\\") || value.includes("\0")) || /[*?\[\]]/.test(value)
          || value.split("/").some(v => ["", "..", ".", ".git", ".autodev", ".specify", "specs"].includes(v))) {
        errors.push(`工作包 ${key} 修改范围必须为安全的精确相对路径：${raw}`); continue;
      }
      if (["docs/SRS.md", "docs/CONSTITUTION.md", "docs/ARCHITECTURE.md"].some(v => overlap(value, v)))
        errors.push(`工作包 ${key} 不能修改只读规格汇总文档：${raw}`);
      if (p.role !== "contract") for (const protectedPath of schema["x-vernal-protected-paths"] || []) {
        const prefix = protectedPath.replace(/[/*]+$/, "");
        if (prefix && overlap(value, prefix)) errors.push(`工作包 ${key} 不能写入受保护路径：${raw} 与 ${protectedPath}`);
      }
      for (const existing of schema["x-vernal-existing-paths"] || [])
        if (overlap(value, existing)) errors.push(`工作包 ${key} 修改范围与已有任务重叠：${raw} 与 ${existing}`);
      for (const [other, territory] of territories)
        if (other !== key && overlap(value, territory)) errors.push(`工作包 ${other} 与 ${key} 修改范围重叠：${territory} 与 ${raw}`);
      territories.push([key, value]);
      if (root !== null) { try { safeArtifactPath(root, value); } catch(e) { errors.push(`工作包 ${key} ${e.message}`); } }
    }
    if (!p.steps.length) errors.push(`工作包 ${key} steps 不能为空`);
    for (const step of p.steps) {
      const task = step.task_key;
      if (!/^T[0-9]{3,}$(?![\s\S])/.test(task) || steps.has(task)) errors.push(`工作包 ${key} 步骤 ${task} 编号无效或重复`);
      steps.set(task, step); owners.set(task, key);
      if (!strip(step.title) || [...step.title].length > 120 || !strip(step.description)) errors.push(`工作包 ${key} 步骤 ${task} 标题或说明无效`);
      const missing = [...new Set(step.requirement_ids)].filter(r => !reqIds.has(r)).sort();
      if (!step.requirement_ids.length || missing.length) errors.push(`工作包 ${key} 步骤 ${task} 引用的需求不存在或为空：` + missing.join(", "));
      step.requirement_ids.forEach(r => covered.add(r));
      for (const cmd of step.verify_cmds) if (!commandValid(cmd)) errors.push(`工作包 ${key} 步骤 ${task} 验证命令为空或引号未闭合：${cmd}`);
    }
  }
  const missing = [...new Set(schema["x-vernal-p0-req-ids"] || [])].filter(r => !covered.has(r)).sort();
  if (missing.length) errors.push("工作包 " + [...keys].sort().join(", ") + " 未覆盖 P0 需求：" + missing.join(", "));
  const stepDeps = new Map([...steps].map(([key, step]) => [key, new Set(step.depends_on)]));
  for (const [task, refs] of stepDeps) for (const ref of [...refs].sort()) {
    if (!steps.has(ref)) errors.push(`工作包 ${owners.get(task)} 步骤 ${task} 依赖不存在：${ref}`);
    else if (owners.get(task) !== owners.get(ref)) dependencies.get(owners.get(task)).add(owners.get(ref));
  }
  for (const [key, refs] of dependencies) for (const ref of [...refs].filter(r => !keys.has(r)).sort())
    errors.push(`工作包 ${key} 依赖不存在：${ref}`);
  for (const [graph, label] of [[stepDeps, "步骤"], [dependencies, "工作包"]])
    if ([...graph.values()].every(refs => [...refs].every(r => graph.has(r)))) {
      try { ordered(graph, label); } catch(e) { errors.push(e.message); }
    }
  if (schema["x-vernal-single-milestone"] && new Set(packages.map(p => p.milestone_id)).size !== 1)
    errors.push("工作包 " + [...keys].sort().join(", ") + " 必须属于同一里程碑");
  if (errors.length) throw Error(errors.join("\n"));
  return dependencies;
}
function liteSections(text, names) {
  const sections = new Map();
  for (const chunk of text.replace(/\r\n?/g, "\n").split(/^## /m).slice(1)) {
    const [headingLine, ...body] = chunk.split("\n");
    const name = headingLine.replace(new RegExp(SPACE + "+$", "u"), ""), content = strip(body.join("\n"));
    if (!names.includes(name)) continue;
    if (sections.has(name) || !content) throw Error("文档缺少唯一非空章节：" + name);
    sections.set(name, content);
  }
  for (const name of names) if (!sections.has(name)) throw Error("文档缺少唯一非空章节：" + name);
  return sections;
}
function validateLiteBundle(data, schema) {
  function requireValue(ok, message) { if (!ok) throw Error(message); }
  if (schema["x-vernal-public-pattern"]) {
    const pattern = new RegExp(schema["x-vernal-public-pattern"].replace("\\bissue-", "(?<![\\p{L}\\p{N}_])issue-"), "u");
    const texts = [data.spec_markdown, data.plan_markdown, data.rationale, ...Object.values(data.handoff_brief),
      canonicalJson(data.work_packages), ...data.clarifications.flatMap(q => [q.question, q.rationale, ...q.options])];
    requireValue(!texts.some(s => pattern.test(s)), "对外文档包含内部执行协议");
  }
  const {mode, clarifications: questions, work_packages: packages} = data;
  requireValue(Object.values(data.handoff_brief).every(strip), "交接便签不得为空");
  requireValue(new Set(data.prerequisites.map(p => p.key)).size === data.prerequisites.length, "前置条件编号重复");
  if (mode === "NEEDS_CLARIFICATION") {
    requireValue(questions.length >= 1 && questions.length <= 3 && !packages.length, "待澄清模式需要一至三个阻塞问题且工作包为空");
    requireValue(new Set(questions.map(q => q.question_key)).size === questions.length, "澄清问题编号重复");
    for (const q of questions)
      requireValue(q.severity === "BLOCKING" && new Set(q.options).size === q.options.length &&
        q.options.every(strip) && (!q.default_option || q.options.includes(q.default_option)), "澄清问题或选项无效");
    return;
  }
  requireValue(!questions.length, "仅待澄清模式可提供问题");
  if (mode === "ESCALATE_STANDARD") {
    if (data.risk === "low") requireValue(!["", "low", "低风险", "风险低"].includes(strip(data.rationale).toLowerCase()), "低风险升级须明确说明轻量边界");
    requireValue(!packages.length && strip(data.rationale), "升级标准通道须说明原因且不发布工作包");
    return;
  }
  requireValue(mode === "READY_FOR_APPROVAL" && data.risk === "low" && packages.length >= 1 && packages.length <= 2,
    "批准模式须为低风险且包含一至两个工作包");
  const [ids, p0] = specRequirements(data.spec_markdown);
  liteSections(data.plan_markdown, ["需求映射","接口与数据","实施边界","验证"]);
  validateWorkPackages(packages, {...schema, "x-vernal-req-ids":ids, "x-vernal-p0-req-ids":p0, "x-vernal-single-milestone":true}, process.cwd());
}
function specRequirements(text) {
  function requireValue(ok, message) { if (!ok) throw Error(message); }
  const spec = liteSections(text, ["目标","用户场景","Non-Goals","需求"]);
  for (const name of ["用户场景","Non-Goals"])
    requireValue(spec.get(name).split("\n").some(s => s.startsWith("- ") && strip(s.slice(2))), name + " 必须包含明确条目");
  const ids = [], p0 = [], chunks = spec.get("需求").split(/^### /m).slice(1);
  requireValue(chunks.length, "规格缺少有效需求条目");
  for (const chunk of chunks) {
    const [title, ...rest] = chunk.split("\n");
    const header = /^((?:REQ|NFR)-[0-9]{3,}) \[(P[012])\] (.+)$/u.exec(title);
    requireValue(header && !ids.includes(header[1]), "规格需求标题无效或编号重复");
    const body = strip(rest.join("\n").split(/^#{1,2} /m)[0]);
    const mark = "\n验收：\n", offset = body.indexOf(mark), statement = body.slice(0, offset);
    const lines = body.slice(offset + mark.length).split("\n").filter(strip);
    requireValue(offset >= 0 && statement.startsWith("要求：") && strip(statement.slice(3)) &&
      lines.every(s => s.startsWith("- ")) && lines.some(s => strip(s.slice(2))), header[1] + " 缺少有效要求或逐条验收条件");
    ids.push(header[1]);
    if (header[2] === "P0") p0.push(header[1]);
  }
  return [ids, p0];
}
function renderPackages(packages) {
  const lines = ["<!-- vernal-tasks-schema: v2 -->", "# 任务草案", ""];
  for (const p of packages) {
    lines.push(`## ${p.package_key} ${p.title}`, `里程碑：${p.milestone_id}；组：${p.squad}；模块：${p.module}；角色：${p.role}`,
      "", "### 验收条件", ...p.acceptance_criteria.map(x => "- " + x), "", "### 修改范围", ...p.allowed_paths.map(x => "- " + x),
      "", "### 验证命令", ...p.verify_cmds.map(x => "- " + x), "", "工作包依赖：" + (p.depends_on_packages.join(", ") || "无"), "", "### 实施步骤");
    for (const s of p.steps) {
      lines.push(`- [ ] ${s.task_key} ${s.title}`, "  " + s.description, "  需求：" + s.requirement_ids.join(", "), "  依赖：" + (s.depends_on.join(", ") || "无"));
      lines.push(...s.verify_cmds.map(cmd => "  验证：" + cmd));
    }
    lines.push("");
  }
  return lines.join("\n");
}

function main() {
const data = JSON.parse(fs.readFileSync("vernal-result.json", "utf8"));
if (!data || typeof data !== "object" || Array.isArray(data) || !Object.keys(data).length) throw Error("规划产物必须为非空对象");
if ("mode" in data && "work_packages" in data && !fs.existsSync(".vernal-schema.json")) throw Error("lite_bundle 缺少 .vernal-schema.json");
if (data.step === "tasks" && !fs.existsSync(".vernal-schema.json")) throw Error("tasks 缺少 .vernal-schema.json");
if (fs.existsSync(".vernal-schema.json")) {
  const schema = JSON.parse(fs.readFileSync(".vernal-schema.json", "utf8"));
  validate(data, schema);
  const step=schema["x-vernal-step"], text=(step === "std_plan" ? data.plan_markdown : data.content)||"";
  if (step === "std_spec") {
    specRequirements(data.proposed_spec);
    if (!(data.goal_clear && data.scope_clear && data.acceptance_clear) && !data.questions.some(q => q.severity === "BLOCKING"))
      throw Error("交付承诺仍不清楚时必须提供待确认问题");
    if (data.questions.some(q => q.severity === "DEFAULTED" && ["scope", "acceptance", "non_goal"].includes(q.category)))
      throw Error("范围与验收承诺不能作为低风险默认值");
    if (data.experience_items.some(x => x.core_acceptance)) throw Error("核心验收不能降级为交付后人工体验项");
    if (new Set(data.prerequisites.map(p => p.key)).size !== data.prerequisites.length) throw Error("前置条件编号重复");
  }
  if (["std_plan", "std_spec"].includes(step) && !Object.values(data.handoff_brief).every(strip))
    throw Error("交接便签不得为空");
  if (step === "std_plan") {
    liteSections(data.plan_markdown, ["需求映射","接口与数据","实施边界","验证"]);
    validateWorkPackages(data.work_packages, schema, process.cwd());
  }
  if (step === "lite_bundle") validateLiteBundle(data, schema);
  for(const heading of ({specify:["目标","用户场景","Non-Goals","需求"],plan:["需求映射","接口与数据","实施边界","验证"]}[step]||[])) {
    if(!new RegExp("^## "+heading+"\\s*\\n\\s*\\S","m").test(text)) throw Error("文档缺少非空章节："+heading);
  }
  const reqFindingIds = Array.isArray(schema["x-vernal-require-findings-ids"]) ? schema["x-vernal-require-findings-ids"] : [];
  if (["plan", "std_plan"].includes(step) && reqFindingIds.length) {
    const start = text.match(/^## 检查发现处理\s*\n/m);
    if (!start) throw Error("文档缺少非空章节：检查发现处理");
    const rest = text.slice(start.index + start[0].length);
    const nextIdx = rest.search(/^## /m);
    const body = (nextIdx >= 0 ? rest.slice(0, nextIdx) : rest).trim();
    if (!body) throw Error("文档缺少非空章节：检查发现处理");
    const missing = reqFindingIds.filter(fid => !body.includes(fid));
    if (missing.length) throw Error("检查发现处理章节缺少质量发现标识：" + missing.join(", "));
  }
  if(step==="specify" && !/^### (?:REQ|NFR)-\d{3,} \[P[012]\] [^\n]+\n要求：[^\n]+\n验收：\n- \S/m.test(text)) throw Error("规格缺少有效需求与验收条目");
  if(step==="clarify" && (strip(text) || data.clarifications.length>3)) throw Error("澄清只返回至多三个问题，不改写正文");
  if (step === "tasks") {
    if (!schema["x-vernal-artifact-path"]) throw Error("tasks 缺少产物路径契约");
    validateWorkPackages(data.work_packages, schema, process.cwd());
    data.content = renderPackages(data.work_packages);
  }
  if ("x-vernal-artifact-path" in schema) {
    if (Object.values(data.handoff_brief).some(v => !strip(String(v)))) throw Error("交接便签不得为空");
    const root = process.cwd(), target = schema["x-vernal-artifact-path"], writes = new Map();
    if (target) {
      if (!strip(data.content)) throw Error("产物正文或路径无效");
      writes.set(safeArtifactPath(root, target), data.content);
    }
    if (step === "tasks") {
      const tasksPath = schema["x-vernal-tasks-json-path"] || "";
      if (!/^specs\/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\/tasks[.]json$/.test(tasksPath) || target !== tasksPath.slice(0, -4) + "md")
        throw Error("tasks.json 与 tasks.md 产物路径不匹配");
      writes.set(safeArtifactPath(root, tasksPath), canonicalJson(data.work_packages));
    }
    safeArtifactPath(root, "vernal-result.json");
    for (const [full, content] of writes) {
      fs.mkdirSync(path.dirname(full), {recursive:true});
      fs.writeFileSync(full, content, "utf8");
    }
  }
}
fs.writeFileSync(safeArtifactPath(process.cwd(), "vernal-result.json"), canonicalJson(data), "utf8");
console.log("结构化产物与文档格式校验通过；业务验证结果以交接便签为准");

}
try { main(); } catch (e) { console.error(e.message); process.exitCode = 1; }
