const fs = require("fs");
const path = require("path");
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
  if (checks[schema.type] && !checks[schema.type]()) throw Error(`${where}: 类型无效`);
  if (schema.enum && !schema.enum.includes(value)) throw Error(`${where}: 不属于允许值`);
  if ("const" in schema && value !== schema.const) throw Error(`${where}: 固定值不匹配`);
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const props = schema.properties || {};
    for (const name of schema.required || []) if (!(name in value)) throw Error(`${where}: 缺少 ${name}`);
    for (const [name, item] of Object.entries(value)) {
      if (!(name in props) && schema.additionalProperties === false) throw Error(`${where}: 未声明字段 ${name}`);
      if (name in props) validate(item, props[name], `${where}.${name}`);
    }
  }
  if (Array.isArray(value)) value.forEach((item, i) => validate(item, schema.items || {}, `${where}[${i}]`));
  if (typeof value === "string" && (value.length < (schema.minLength || 0) ||
      (schema.pattern && !new RegExp(schema.pattern).test(value)))) throw Error(`${where}: 文本格式无效`);
}
const data = JSON.parse(fs.readFileSync("vernal-result.json", "utf8"));
if (!data || typeof data !== "object" || Array.isArray(data) || !Object.keys(data).length) throw Error("规划产物必须为非空对象");
if (fs.existsSync(".vernal-schema.json")) {
  const schema = JSON.parse(fs.readFileSync(".vernal-schema.json", "utf8"));
  validate(data, schema);
  const step=schema["x-vernal-step"], text=data.content||"";
  for(const heading of ({specify:["目标","用户场景","Non-Goals","需求"],plan:["需求映射","接口与数据","实施边界","验证"]}[step]||[])) {
    if(!new RegExp("^## "+heading+"\\s*\\n\\s*\\S","m").test(text)) throw Error("文档缺少非空章节："+heading);
  }
  if(step==="specify" && !/^### (?:REQ|NFR)-\d{3,} \[P[012]\] [^\n]+\n要求：[^\n]+\n验收：\n- \S/m.test(text)) throw Error("规格缺少有效需求与验收条目");
  if(step==="clarify" && (text.trim() || data.clarifications.length>3)) throw Error("澄清只返回至多三个问题，不改写正文");
  if(step==="tasks") {
    const blocks=[...text.matchAll(/^- \[ \] (T\d{3,}) ([^\n]+)\n```vernal-task\n(.*?)\n```/gms)];
    if(!blocks.length || blocks.length!==(text.match(/^- \[/gm)||[]).length) throw Error("任务条目与结构化元数据不完整");
    for(const block of blocks) { const item=JSON.parse(block[3]); validate(item,schema["x-vernal-task-schema"]);
      if(item.task_key!==block[1] || item.title!==block[2].trim()) throw Error("任务编号或标题与元数据不一致"); }
  }
  if ("x-vernal-artifact-path" in schema) {
    if (Object.values(data.handoff_brief).some(v => !String(v).trim())) throw Error("交接便签不得为空");
    const rel = schema["x-vernal-artifact-path"];
    if (rel) {
      const root = process.cwd();
      const full = path.resolve(root, rel);
      if (path.isAbsolute(rel) || rel.split("/").includes("..") || !full.startsWith(root + path.sep) || !data.content.trim()) throw Error("产物路径或正文无效");
      for (let cursor=path.dirname(full); cursor!==root; cursor=path.dirname(cursor)) {
        if (fs.existsSync(cursor) && fs.lstatSync(cursor).isSymbolicLink()) throw Error("产物路径包含符号链接");
      }
      fs.mkdirSync(path.dirname(full), {recursive:true});
      if (!fs.realpathSync(path.dirname(full)).startsWith(root + path.sep) || (fs.existsSync(full) && fs.lstatSync(full).isSymbolicLink())) throw Error("产物路径越界");
      fs.writeFileSync(full, data.content, "utf8");
    }
  }
}
fs.writeFileSync("vernal-result.json", JSON.stringify(data, null, 2), "utf8");
console.log("结构化产物与文档格式校验通过；业务验证结果以交接便签为准");
