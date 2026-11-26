"""Offline assets, verified before use and copied into a project-specific immutable lock."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

ASSET_ROOT = Path(__file__).parents[1] / "assets" / "speckit"


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
            "instructions": core["instructions"] + "\n" + core["steps"][step]
                            + ("\n" + selected["instructions"] if selected != core else "")}
