"""Standalone standard-library validator embedded in the planning SOP script node."""
import json
import pathlib
import re


def validate(value, schema, path="result"):
    if "anyOf" in schema:
        errors = []
        for choice in schema["anyOf"]:
            try:
                validate(value, choice, path)
                return
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(f"{path}: 不满足任一允许类型")
    kind = schema.get("type")
    checks = {"object":lambda: isinstance(value,dict),"array":lambda:isinstance(value,list),
              "string":lambda:isinstance(value,str),"integer":lambda:type(value) is int,
              "number":lambda:type(value) in (int,float),"boolean":lambda:type(value) is bool,
              "null":lambda:value is None}
    if kind in checks and not checks[kind]():
        raise ValueError(f"{path}: 需要 {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: 不属于允许值")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: 固定值不匹配")
    if isinstance(value,dict):
        props = schema.get("properties",{})
        if missing := set(schema.get("required",[]))-value.keys():
            raise ValueError(f"{path}: 缺少字段 {sorted(missing)}")
        if schema.get("additionalProperties") is False and value.keys()-props.keys():
            raise ValueError(f"{path}: 存在未声明字段")
        for key,item in value.items():
            if key in props:
                validate(item,props[key],f"{path}.{key}")
    if isinstance(value,list):
        for i,item in enumerate(value):
            validate(item,schema.get("items",{}),f"{path}[{i}]")
    if isinstance(value,str):
        if len(value)<schema.get("minLength",0) or ("pattern" in schema and not re.search(schema["pattern"],value)):
            raise ValueError(f"{path}: 文本格式无效")


def main():
    root=pathlib.Path.cwd()
    output=root/"vernal-result.json"
    if not output.is_file():
        raise ValueError("缺少 vernal-result.json")
    data=json.loads(output.read_text("utf-8"))
    if not isinstance(data,dict) or not data:
        raise ValueError("规划产物必须为非空对象")
    source=root/".vernal-schema.json"
    if source.is_file():
        schema=json.loads(source.read_text("utf-8"))
        validate(data,schema)
        step=schema.get("x-vernal-step")
        text=data.get("content","")
        for heading in ({"specify":["目标","用户场景","Non-Goals","需求"],"plan":["需求映射","接口与数据","实施边界","验证"]}.get(step,[])):
            if not re.search(r"^## " + re.escape(heading) + r"\s*\n\s*\S",text,re.M):
                raise ValueError("文档缺少非空章节："+heading)
        if step=="specify" and not re.search(r"^### (?:REQ|NFR)-\d{3,} \[P[012]\] [^\n]+\n要求：[^\n]+\n验收：\n- \S",text,re.M):
            raise ValueError("规格缺少有效需求与验收条目")
        if step=="clarify" and (text.strip() or len(data["clarifications"])>3):
            raise ValueError("澄清只返回至多三个问题，不改写正文")
        if step=="tasks":
            blocks=list(re.finditer(r"^- \[ \] (T\d{3,}) ([^\n]+)\n```vernal-task\n(.*?)\n```",text,re.M|re.S))
            if not blocks or len(blocks)!=len(re.findall(r"^- \[",text,re.M)):
                raise ValueError("任务条目与结构化元数据不完整")
            for block in blocks:
                item=json.loads(block[3]);validate(item,schema["x-vernal-task-schema"])
                if item["task_key"]!=block[1] or item["title"]!=block[2].strip():
                    raise ValueError("任务编号或标题与元数据不一致")
        target=schema.get("x-vernal-artifact-path")
        if "x-vernal-artifact-path" in schema:
            if any(not str(v).strip() for v in data["handoff_brief"].values()):
                raise ValueError("交接便签不得为空")
            if target:
                rel=pathlib.PurePosixPath(target)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError("产物路径越界")
                full=(root/target).resolve()
                if not full.is_relative_to(root.resolve()) or not data["content"].strip():
                    raise ValueError("产物正文或路径无效")
                full.parent.mkdir(parents=True,exist_ok=True)
                full.write_text(data["content"],"utf-8")
    output.write_text(json.dumps(data,ensure_ascii=False,indent=2),"utf-8")
    print("结构化产物与文档格式校验通过；业务验证结果以交接便签为准")


if __name__ == "__main__":
    main()
