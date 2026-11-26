"""Offline assets, verified before use and copied into a project-specific immutable lock."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

ASSET_ROOT = Path(__file__).parents[1] / "assets" / "speckit"
PROMPT_ASSEMBLY_VERSION = "v2-concise-20261126"

STEP_METHODOLOGY: dict[str, str] = {
    "constitution": (
        "方法要点：沉淀项目级不可违背原则（技术约束、兼容基线、测试红线与变更治理），"
        "条文须可核验且简洁；不要在宪章重复列举具体功能 REQ 或虚构未经确认的 P0 业务承诺。"
    ),
    "specify": (
        "方法要点：聚焦用户目标、场景、明确 Non-Goals 与可测试 REQ/NFR（EARS 句式+独立可观察验收）。"
        "适用的宪章硬性原则（如兼容基线）须在本规格登记为 NFR；禁止占位符和未经用户确认的业务猜测。"
    ),
    "clarify": (
        "方法要点：只针对影响交付承诺的关键歧义提出最多 3 个高杠杆问题（范围/数据/错误/验收），"
        "给出有限选项与依据；已有答案严禁重复提问，无关键歧义返回空数组，不修改规格正文。"
    ),
    "checklist": (
        "方法要点：执行规格质量审查（非程序测试），按完备性、清晰度、一致性、可测性、边界与异常、"
        "以及宪章适用 NFR 是否已登记到规格逐项核对；可操作问题写入 findings（区分 CRITICAL/WARNING/INFO）。"
    ),
    "plan": (
        "方法要点：基于已批准规格与当前质量发现制定最小可实施技术方案，包含需求映射、接口与数据、实施边界、验证"
        "（有质量发现时必含「## 检查发现处理」表）。不改写已批准业务承诺，需变承诺的标明退回规格修订。"
    ),
    "tasks": (
        "方法要点：按模块与验收边界将方案拆解为可执行微步骤，并通过 package_key 归并为自包含工作包。"
        "覆盖全部 P0 REQ 与适用 P0 NFR，路径明确且跨工作包不重叠，验证命令可直接执行且不得预勾完成。"
    ),
    "analyze": (
        "方法要点：只读交叉核验宪章、规格、当前质量发现、技术方案与任务草案的一致性。"
        "逐条核对每条 CHK 质量发现是否在需求/接口/任务/验证中真正落实；遗漏 P0、矛盾或不可测承诺标记 CRITICAL。"
    ),
    "intake": (
        "方法要点：独立判定目标/范围/验收清晰度、影响面与风险；不按字数或项目来源猜测。"
        "仅追问影响交付承诺的未决事项（最多 3 题，可为 0 题），未知账号/硬件/环境依赖归入 prerequisites，"
        "结合已有澄清回答输出或更新规范的 proposed_spec，保留既有需求编号与用户确认原文。"
    ),
}


def step_methodology_summary(step: str) -> str:
    return STEP_METHODOLOGY.get(step, "")


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_bundle(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    if manifest.get("schema_version") != 1 or not manifest.get("files"):
        raise ValueError("不支持的规格资产清单")
    for rel, sha in manifest["files"].items():
        p = root / rel
        if Path(rel).is_absolute() or ".." in Path(rel).parts or p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("非法的资产清单路径")
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise ValueError(f"规格资产校验失败：{rel}")
    return manifest


def _stage_bundle(engine, manifest: dict) -> str:
    key = digest_json(manifest)
    parent = engine.ws.state_dir / "speckit-assets"
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / key
    if not dest.exists():
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            staged = Path(temp) / "bundle"
            shutil.copytree(ASSET_ROOT, staged)
            verify_bundle(staged)
            try:
                staged.rename(dest)
            except FileExistsError:
                verify_bundle(dest)
    return key


def pin_assets(engine, preset_id: str = "vernal-core") -> dict:
    existing = engine.db.meta_get("speckit.assets")
    if existing:
        locked = json.loads(existing)
        if locked["preset_id"] != preset_id:
            raise ValueError("项目已锁定其他规格预设，需通过显式资产升级流程变更")
        load_assets(engine)
        return locked
    manifest = verify_bundle(ASSET_ROOT)
    if preset_id not in manifest["presets"]:
        raise ValueError("当前仅支持 vernal-core 与 vernal-lite 预设")
    key = _stage_bundle(engine, manifest)
    locked = {"asset_sha": key, "speckit_version": manifest["speckit_version"],
              "upstream_commit": manifest["upstream_commit"], "preset_id": preset_id,
              "preset_version": manifest["presets"][preset_id]["version"]}
    with engine.db.transaction():
        current = engine.db.meta_get("speckit.assets")
        if current and json.loads(current) != locked:
            raise ValueError("规格资产已被并发操作锁定，请刷新")
        engine.db.meta_set("speckit.assets", json.dumps(locked, ensure_ascii=False))
    return locked


def upgrade_assets(engine, target_preset_id: str | None = None, *, actor: str = "owner", reason: str = "") -> dict:
    """Explicitly lock a newer repository asset bundle or preset without mutating prior immutable locks."""
    from autodev.db import now_iso
    from .service import SpecConflictError

    raw = engine.db.meta_get("speckit.assets")
    if not raw:
        raise ValueError("请先启用功能规格流程")
    if engine.db.meta_get("mission.lease"):
        raise SpecConflictError("需求流程正在执行，不能升级规格资产")
    if engine.db._query("SELECT 1 FROM dispatch_records WHERE state IN ('DISPATCHING','QUEUED','RUNNING','NEEDS_CONFIRMATION')"):
        raise SpecConflictError("仍有原生执行未结束，不能升级规格资产")
    if any(t.status in ("IN_PROGRESS", "VERIFYING", "REVIEWING") for t in engine.db.list_tasks()):
        raise SpecConflictError("仍有研发任务运行，不能升级规格资产")
    previous, _ = load_assets(engine)
    preset_id = target_preset_id or previous["preset_id"]
    manifest = verify_bundle(ASSET_ROOT)
    if preset_id not in manifest["presets"]:
        raise ValueError("当前仅支持 vernal-core 与 vernal-lite 预设")
    key = _stage_bundle(engine, manifest)
    updated = {
        "asset_sha": key,
        "speckit_version": manifest["speckit_version"],
        "upstream_commit": manifest["upstream_commit"],
        "preset_id": preset_id,
        "preset_version": manifest["presets"][preset_id]["version"],
    }
    if updated == previous:
        return {"previous": previous, "current": updated, "changed": False}
    with engine.db.transaction():
        if json.loads(engine.db.meta_get("speckit.assets") or "{}") != previous:
            raise SpecConflictError("规格资产锁定状态已变化，请刷新后重试")
        history = json.loads(engine.db.meta_get("speckit.assets.history") or "[]")
        history.append({
            "from": previous,
            "to": updated,
            "actor": actor,
            "reason": reason.strip(),
            "at": now_iso(),
        })
        engine.db.meta_set("speckit.assets", json.dumps(updated, ensure_ascii=False))
        engine.db.meta_set("speckit.assets.history", json.dumps(history, ensure_ascii=False))
        engine.db.add_event(
            "speckit.assets_upgraded",
            f"规格资产已显式升级：{previous['preset_id']}@{previous['preset_version']} -> {updated['preset_id']}@{updated['preset_version']}",
            actor=actor,
            payload={"from": previous, "to": updated, "reason": reason.strip()},
        )
    return {"previous": previous, "current": updated, "changed": True}


def load_assets(engine) -> tuple[dict, Path]:
    raw = engine.db.meta_get("speckit.assets")
    if not raw:
        raise ValueError("请先启用功能规格流程")
    locked = json.loads(raw)
    key = locked["asset_sha"]
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("无效的资产锁")
    root = engine.ws.state_dir / "speckit-assets" / key
    manifest = verify_bundle(root)
    if digest_json(manifest) != key:
        raise ValueError("项目规格资产清单与锁定版本不符")
    return locked, root


def step_assets(engine, step: str) -> dict:
    if step not in ("constitution", "specify", "clarify", "plan", "tasks", "checklist", "analyze"):
        raise ValueError("不支持的规格步骤")
    locked, root = load_assets(engine)
    upstream = root / "upstream" / f"v{locked['speckit_version']}" / "templates"
    template = {"specify": "spec", "clarify": "spec", "analyze": "checklist"}.get(step, step)
    selected = json.loads((root / "presets" / f"{locked['preset_id']}.json").read_text("utf-8"))
    core = json.loads((root / "presets" / "vernal-core.json").read_text("utf-8"))
    return {"versions": locked, "upstream_command": (upstream / "commands" / f"{step}.md").read_text("utf-8"),
            "template": (upstream / f"{template}-template.md").read_text("utf-8"),
            "methodology": step_methodology_summary(step),
            "prompt_template_version": PROMPT_ASSEMBLY_VERSION,
            "instructions": core["instructions"] + "\n" + core["steps"][step]
                            + ("\n" + selected["instructions"] if selected != core else "")}
