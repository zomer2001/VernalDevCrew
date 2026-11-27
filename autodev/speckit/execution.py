"""Project-local references to existing native planning runs."""
import json
from .paths import feature_spec_dir


def record(db, feature, run):
    feature_spec_dir(feature)
    db.meta_set(f"speckit.execution:{run['run_id']}", json.dumps({**run, "feature_id": feature}, ensure_ascii=False))


def history(engine, feature):
    feature_spec_dir(feature)
    entries = [json.loads(row["value"]) for row in engine.db._query(
        "SELECT value FROM meta WHERE key LIKE 'speckit.execution:%'")]
    found = {item["run_id"]: item for item in entries if item["feature_id"] == feature}
    # Completed runs created before the live index retain their signed dispatch
    # metadata in the existing project log directory.
    root = engine.ws.runs_dir / "speckit" / feature
    for path in root.glob("*/*/loopforge_run.json"):
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            item = json.loads(path.read_text("utf-8"))
            rid = item.get("run_id")
            if rid and rid not in found:
                found[rid] = {**item, "feature_id": feature, "round": 0, "started_at": ""}
        except (OSError, ValueError):
            continue
    return sorted(found.values(), key=lambda item: item["run_id"], reverse=True)[:50]
