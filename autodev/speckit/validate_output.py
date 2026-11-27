"""Standalone standard-library validator embedded in the planning SOP script node."""
import json
import pathlib
import re
import sys


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
        if len(value)<schema.get("minItems",0) or ("maxItems" in schema and len(value)>schema["maxItems"]):
            raise ValueError(f"{path}: 数组长度无效")
        for i,item in enumerate(value):
            label = (" 工作包 " + str(item["package_key"]) if isinstance(item, dict) and "package_key" in item else
                     " 步骤 " + str(item["task_key"]) if isinstance(item, dict) and "task_key" in item else "")
            validate(item,schema.get("items",{}),f"{path}[{i}]{label}")
    if isinstance(value,str):
        pattern = schema.get("pattern", "")
        if pattern.endswith("$"):
            pattern = pattern[:-1] + r"\Z"
        if len(value)<schema.get("minLength",0) or ("maxLength" in schema and len(value)>schema["maxLength"]) or ("pattern" in schema and not re.search(pattern,value)):
            raise ValueError(f"{path}: 文本格式无效")

def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def overlap(a, b):
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def command_valid(command):
    """Basic shell lexical pairing, deliberately not shell execution or parsing."""
    if not isinstance(command, str) or not command.strip():
        return False
    quote = ""
    escaped = False
    token = False
    for char in command:
        if escaped:
            escaped = False
            token = True
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
            else:
                token = True
        elif char in ("'", '"'):
            quote = char
        elif not char.isspace():
            token = True
    return not quote and not escaped and token


def ordered(dependencies, label):
    pending = {key: set(values) for key, values in dependencies.items()}
    result = []
    while pending:
        ready = sorted(key for key, values in pending.items() if not values)
        if not ready:
            raise ValueError(label + "依赖形成循环：" + ", ".join(sorted(pending)))
        result.extend(ready)
        for key in ready:
            del pending[key]
        for values in pending.values():
            values.difference_update(ready)
    return result


def safe_artifact_path(root, relative):
    if (not isinstance(relative, str) or not relative or relative.startswith("/")
            or "\\" in relative or "\0" in relative or re.match(r"^[A-Za-z]:", relative)
            or any(part in ("", ".", "..", ".git", ".autodev") for part in relative.split("/"))):
        raise ValueError("产物路径越界：" + str(relative))
    target = root / relative
    cursor = root
    for part in relative.split("/"):
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("产物路径包含符号链接：" + relative)
    return target


def validate_work_packages(packages, schema, root=None):
    """The native validator and the platform compiler share this exact rule set."""
    if not isinstance(packages, list) or not packages:
        raise ValueError("tasks 必须返回非空 work_packages")
    errors, keys, steps, owners, covered, territories = [], set(), {}, {}, set(), []
    dependencies = {}
    req_ids = set(schema.get("x-vernal-req-ids", []))
    for package in packages:
        key = package["package_key"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", key) or key in keys:
            errors.append(f"工作包 {key} 编号无效或重复")
        keys.add(key)
        dependencies[key] = set(package["depends_on_packages"])
        for field in ("milestone_id", "squad", "module"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", package[field]):
                errors.append(f"工作包 {key} 的 {field} 必须是安全的非空标识")
        if not package["title"].strip():
            errors.append(f"工作包 {key} 标题不能为空")
        for field in ("acceptance_criteria", "allowed_paths", "verify_cmds"):
            if not package[field] or any(not value.strip() for value in package[field]):
                errors.append(f"工作包 {key} 的 {field} 不能为空")
        for cmd in package["verify_cmds"]:
            if not command_valid(cmd):
                errors.append(f"工作包 {key} 验证命令为空或引号未闭合：{cmd}")
        for raw in package["allowed_paths"]:
            value = raw.rstrip("/")
            if (not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
                    or "\\" in value or "\0" in value or any(c in value for c in "*?[]")
                    or any(p in ("", "..", ".", ".git", ".autodev", ".specify", "specs") for p in value.split("/"))):
                errors.append(f"工作包 {key} 修改范围必须为安全的精确相对路径：{raw}")
                continue
            if any(overlap(value, p) for p in ("docs/SRS.md", "docs/CONSTITUTION.md", "docs/ARCHITECTURE.md")):
                errors.append(f"工作包 {key} 不能修改只读规格汇总文档：{raw}")
            if package["role"] != "contract":
                for protected in schema.get("x-vernal-protected-paths", []):
                    prefix = protected.rstrip("/*")
                    if prefix and overlap(value, prefix):
                        errors.append(f"工作包 {key} 不能写入受保护路径：{raw} 与 {protected}")
            for existing in schema.get("x-vernal-existing-paths", []):
                if overlap(value, existing):
                    errors.append(f"工作包 {key} 修改范围与已有任务重叠：{raw} 与 {existing}")
            for other, path in territories:
                if other != key and overlap(value, path):
                    errors.append(f"工作包 {other} 与 {key} 修改范围重叠：{path} 与 {raw}")
            territories.append((key, value))
            if root is not None:
                try:
                    safe_artifact_path(root, value)
                except ValueError as exc:
                    errors.append(f"工作包 {key} {exc}")
        if not package["steps"]:
            errors.append(f"工作包 {key} steps 不能为空")
        for step in package["steps"]:
            task = step["task_key"]
            if not re.fullmatch(r"T[0-9]{3,}", task) or task in steps:
                errors.append(f"工作包 {key} 步骤 {task} 编号无效或重复")
            steps[task], owners[task] = step, key
            if not step["title"].strip() or len(step["title"]) > 120 or not step["description"].strip():
                errors.append(f"工作包 {key} 步骤 {task} 标题或说明无效")
            missing = sorted(set(step["requirement_ids"]) - req_ids)
            if not step["requirement_ids"] or missing:
                errors.append(f"工作包 {key} 步骤 {task} 引用的需求不存在或为空：" + ", ".join(missing))
            covered.update(step["requirement_ids"])
            for cmd in step["verify_cmds"]:
                if not command_valid(cmd):
                    errors.append(f"工作包 {key} 步骤 {task} 验证命令为空或引号未闭合：{cmd}")
    missing = sorted(set(schema.get("x-vernal-p0-req-ids", [])) - covered)
    if missing:
        errors.append("工作包 " + ", ".join(sorted(keys)) + " 未覆盖 P0 需求：" + ", ".join(missing))
    step_deps = {key: set(step["depends_on"]) for key, step in steps.items()}
    for task, refs in step_deps.items():
        for ref in sorted(refs):
            if ref not in steps:
                errors.append(f"工作包 {owners[task]} 步骤 {task} 依赖不存在：{ref}")
            elif owners[task] != owners[ref]:
                dependencies[owners[task]].add(owners[ref])
    for key, refs in dependencies.items():
        for ref in sorted(refs - keys):
            errors.append(f"工作包 {key} 依赖不存在：{ref}")
    for graph, label in ((step_deps, "步骤"), (dependencies, "工作包")):
        if all(ref in graph for refs in graph.values() for ref in refs):
            try:
                ordered(graph, label)
            except ValueError as exc:
                errors.append(str(exc))
    if schema.get("x-vernal-single-milestone") and len({p["milestone_id"] for p in packages}) != 1:
        errors.append("工作包 " + ", ".join(sorted(keys)) + " 必须属于同一里程碑")
    if errors:
        raise ValueError("\n".join(errors))
    return dependencies


def lite_sections(text, names):
    sections = {}
    for chunk in re.split(r"^## ", text.replace("\r\n", "\n").replace("\r", "\n"), flags=re.M)[1:]:
        heading, _, body = chunk.partition("\n")
        heading = heading.rstrip()
        if heading in names:
            if heading in sections or not body.strip():
                raise ValueError("文档缺少唯一非空章节：" + heading)
            sections[heading] = body.strip()
    for name in names:
        if name not in sections:
            raise ValueError("文档缺少唯一非空章节：" + name)
    return sections


def validate_lite_bundle(data, schema, root=None):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    public_pattern = schema.get("x-vernal-public-pattern")
    if public_pattern:
        texts = [data["spec_markdown"], data["plan_markdown"], data["rationale"], *data["handoff_brief"].values(),
                 canonical_json(data["work_packages"])]
        texts.extend(s for q in data["clarifications"] for s in [q["question"], q["rationale"], *q["options"]])
        require(not any(re.search(public_pattern, s) for s in texts), "对外文档包含内部执行协议")
    mode, questions, packages = data["mode"], data["clarifications"], data["work_packages"]
    require(all(v.strip() for v in data["handoff_brief"].values()), "交接便签不得为空")
    keys = [p["key"] for p in data["prerequisites"]]
    require(len(keys) == len(set(keys)), "前置条件编号重复")
    if mode == "NEEDS_CLARIFICATION":
        require(1 <= len(questions) <= 3 and not packages, "待澄清模式需要一至三个阻塞问题且工作包为空")
        require(len({q["question_key"] for q in questions}) == len(questions), "澄清问题编号重复")
        for q in questions:
            options = q["options"]
            require(q["severity"] == "BLOCKING" and len(set(options)) == len(options)
                    and all(s.strip() for s in options) and (not q["default_option"] or q["default_option"] in options),
                    "澄清问题或选项无效")
        return
    require(not questions, "仅待澄清模式可提供问题")
    if mode == "ESCALATE_STANDARD":
        if data["risk"] == "low":
            # A risk label alone gives no reason for exceeding the lite scope.
            reason = data["rationale"].strip().casefold()
            require(reason not in ("", "low", "低风险", "风险低"), "低风险升级须明确说明轻量边界")
        require(not packages and data["rationale"].strip(), "升级标准通道须说明原因且不发布工作包")
        return
    require(mode == "READY_FOR_APPROVAL" and data["risk"] == "low" and 1 <= len(packages) <= 2,
            "批准模式须为低风险且包含一至两个工作包")
    ids, p0 = spec_requirements(data["spec_markdown"])
    lite_sections(data["plan_markdown"], ("需求映射", "接口与数据", "实施边界", "验证"))
    validate_work_packages(packages, {**schema, "x-vernal-req-ids": ids, "x-vernal-p0-req-ids": p0,
                                      "x-vernal-single-milestone": True}, root)


def spec_requirements(text):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    spec = lite_sections(text, ("目标", "用户场景", "Non-Goals", "需求"))
    for name in ("用户场景", "Non-Goals"):
        require(any(s.startswith("- ") and s[2:].strip() for s in spec[name].splitlines()), name + " 必须包含明确条目")
    ids, p0 = [], []
    chunks = re.split(r"^### ", spec["需求"], flags=re.M)[1:]
    require(chunks, "规格缺少有效需求条目")
    for chunk in chunks:
        title, _, body = chunk.partition("\n")
        header = re.fullmatch(r"((?:REQ|NFR)-[0-9]{3,}) \[(P[012])\] (.+)", title)
        require(header and header[1] not in ids, "规格需求标题无效或编号重复")
        body = re.split(r"^#{1,2} ", body, maxsplit=1, flags=re.M)[0].strip()
        statement, separator, checks = body.partition("\n验收：\n")
        lines = [s for s in checks.splitlines() if s.strip()]
        require(separator and statement.startswith("要求：") and statement[3:].strip()
                and all(s.startswith("- ") for s in lines) and any(s[2:].strip() for s in lines),
                header[1] + " 缺少有效要求或逐条验收条件")
        ids.append(header[1])
        if header[2] == "P0":
            p0.append(header[1])
    return ids, p0



def render_packages(packages):
    lines = ["<!-- vernal-tasks-schema: v2 -->", "# 任务草案", ""]
    for package in packages:
        lines += [f"## {package['package_key']} {package['title']}",
                  f"里程碑：{package['milestone_id']}；组：{package['squad']}；模块：{package['module']}；角色：{package['role']}",
                  "", "### 验收条件", *["- " + x for x in package["acceptance_criteria"]],
                  "", "### 修改范围", *["- " + x for x in package["allowed_paths"]],
                  "", "### 验证命令", *["- " + x for x in package["verify_cmds"]],
                  "", "工作包依赖：" + (", ".join(package["depends_on_packages"]) or "无"), "", "### 实施步骤"]
        for step in package["steps"]:
            lines += [f"- [ ] {step['task_key']} {step['title']}", "  " + step["description"],
                      "  需求：" + ", ".join(step["requirement_ids"]),
                      "  依赖：" + (", ".join(step["depends_on"]) or "无")]
            lines += ["  验证：" + cmd for cmd in step["verify_cmds"]]
        lines.append("")
    return "\n".join(lines)


def main():
    root=pathlib.Path.cwd()
    output=root/"vernal-result.json"
    if not output.is_file():
        raise ValueError("缺少 vernal-result.json")
    data=json.loads(output.read_text("utf-8"))
    if not isinstance(data,dict) or not data:
        raise ValueError("规划产物必须为非空对象")
    source=root/".vernal-schema.json"
    if "mode" in data and "work_packages" in data and not source.is_file():
        raise ValueError("lite_bundle 缺少 .vernal-schema.json")
    if data.get("step") == "tasks" and not source.is_file():
        raise ValueError("tasks 缺少 .vernal-schema.json")
    if source.is_file():
        schema=json.loads(source.read_text("utf-8"))
        validate(data,schema)
        step=schema.get("x-vernal-step")
        if step == "lite_bundle":
            validate_lite_bundle(data, schema, root)
        if step == "std_spec":
            spec_requirements(data["proposed_spec"])
            if not all(data[k] for k in ("goal_clear", "scope_clear", "acceptance_clear")) and not any(
                q["severity"] == "BLOCKING" for q in data["questions"]
            ):
                raise ValueError("交付承诺仍不清楚时必须提供待确认问题")
            if any(q["severity"] == "DEFAULTED" and q["category"] in ("scope", "acceptance", "non_goal")
                   for q in data["questions"]):
                raise ValueError("范围与验收承诺不能作为低风险默认值")
            if any(x["core_acceptance"] for x in data["experience_items"]):
                raise ValueError("核心验收不能降级为交付后人工体验项")
            if len({p["key"] for p in data["prerequisites"]}) != len(data["prerequisites"]):
                raise ValueError("前置条件编号重复")
        if step in ("std_plan", "std_spec") and not all(v.strip() for v in data["handoff_brief"].values()):
            raise ValueError("交接便签不得为空")
        if step == "std_plan":
            lite_sections(data["plan_markdown"], ("需求映射", "接口与数据", "实施边界", "验证"))
            validate_work_packages(data["work_packages"], schema, root)
        text=data.get("plan_markdown" if step == "std_plan" else "content","")
        for heading in ({"specify":["目标","用户场景","Non-Goals","需求"],"plan":["需求映射","接口与数据","实施边界","验证"]}.get(step,[])):
            if not re.search(r"^## " + re.escape(heading) + r"\s*\n\s*\S",text,re.M):
                raise ValueError("文档缺少非空章节："+heading)
        req_finding_ids = schema.get("x-vernal-require-findings-ids") or []
        if step in ("plan", "std_plan") and req_finding_ids:
            m = re.search(r"^## 检查发现处理\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
            if not m or not m[1].strip():
                raise ValueError("文档缺少非空章节：检查发现处理")
            missing = [fid for fid in req_finding_ids if fid not in m[1]]
            if missing:
                raise ValueError("检查发现处理章节缺少质量发现标识：" + ", ".join(missing))
        if step=="specify" and not re.search(r"^### (?:REQ|NFR)-\d{3,} \[P[012]\] [^\n]+\n要求：[^\n]+\n验收：\n- \S",text,re.M):
            raise ValueError("规格缺少有效需求与验收条目")
        if step=="clarify" and (text.strip() or len(data["clarifications"])>3):
            raise ValueError("澄清只返回至多三个问题，不改写正文")
        if step == "tasks":
            if not schema.get("x-vernal-artifact-path"):
                raise ValueError("tasks 缺少产物路径契约")
            validate_work_packages(data.get("work_packages"), schema, root)
            data["content"] = render_packages(data["work_packages"])
        target=schema.get("x-vernal-artifact-path")
        if "x-vernal-artifact-path" in schema:
            if any(not str(v).strip() for v in data["handoff_brief"].values()):
                raise ValueError("交接便签不得为空")
            writes = {}
            if target:
                if not data["content"].strip():
                    raise ValueError("产物正文或路径无效")
                writes[safe_artifact_path(root, target)] = data["content"]
            if step == "tasks":
                tasks_path = schema.get("x-vernal-tasks-json-path", "")
                if not re.fullmatch(r"specs/[A-Za-z0-9][A-Za-z0-9_-]{0,79}/tasks[.]json", tasks_path) or target != tasks_path[:-4] + "md":
                    raise ValueError("tasks.json 与 tasks.md 产物路径不匹配")
                writes[safe_artifact_path(root, tasks_path)] = canonical_json(data["work_packages"])
            # Resolve every destination before writing any of the validated artifacts.
            safe_artifact_path(root, "vernal-result.json")
            for full, content in writes.items():
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, "utf-8")
    safe_artifact_path(root, "vernal-result.json").write_text(canonical_json(data), "utf-8")
    print("结构化产物与文档格式校验通过；业务验证结果以交接便签为准")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
