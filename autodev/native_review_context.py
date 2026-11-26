"""Executed as the tail of the self-contained native packaging script."""
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def write_review_context(find_callers):
    import os

    root = Path.cwd()
    if (root / "vernal-patch.json").is_file():
        import base64
        manifest = json.loads((root / "vernal-patch.json").read_text())
        patch = base64.b64decode(manifest["patch"], validate=True)
        if hashlib.sha256(patch).hexdigest() != manifest["sha256"]:
            raise ValueError("无效候选补丁")
        diff = patch.decode(errors="replace")
        impacts = find_callers(root / "vernal-source", diff)
    elif (root / "vernal-files.json").is_file():
        bp = root / ".vernal-baseline.json"
        before = json.loads(bp.read_text()) if bp.is_file() else {}
        changes = json.loads((root / "vernal-files.json").read_text())["files"]
        after = dict(before)
        chunks = []
        for name, value in changes.items():
            if value is None:
                after.pop(name, None)
            else:
                after[name] = value
            chunks.extend(difflib.unified_diff((before.get(name) or "").splitlines(True),
                (value or "").splitlines(True), fromfile="a/" + name, tofile="b/" + name))
        diff = "".join(chunks)
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in after.items():
                path = (Path(tmp) / name).resolve()
                if not path.is_relative_to(Path(tmp).resolve()):
                    raise ValueError("候选路径无效")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            impacts = find_callers(Path(tmp), diff)
    else:
        diff = subprocess.check_output(["git","diff","refs/vernal/review-base","HEAD"], text=True, errors="replace")
        impacts = find_callers(root, diff)
    report = ["# 候选代码审查", "请逐项对照本工单输入包中的详设、关联需求和验收标准。",
              "Diff SHA256 " + hashlib.sha256(diff.encode()).hexdigest(), "## 变更影响面"]
    report.extend(f"{i['defined_in']} · {i['symbol']} · {', '.join(i['callers'])}" for i in impacts)
    text = "\n".join(report) + "\n"
    raw_report = text.encode("utf-8")
    if len(raw_report) > 48_000:
        text = raw_report[:47_800].decode("utf-8", errors="ignore") + "\n\n…（影响面报告已按 48 KB 上限截断）\n"
    (Path(os.environ.get("AGENTFLOW_WORKSPACE", root)) / "vernal-review-context.md").write_text(text, encoding="utf-8")


def spill_large_candidates(max_inline_bytes=48_000):
    import os
    import urllib.request

    root = Path(os.environ.get("AGENTFLOW_WORKSPACE", Path.cwd()))
    upload_url = os.environ.get("VERNAL_CANDIDATE_UPLOAD_URL", "").strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for fname in ("vernal-files.json", "vernal-patch.json"):
        fp = root / fname
        if not fp.is_file():
            continue
        raw = fp.read_bytes()
        if len(raw) <= max_inline_bytes:
            continue
        if not upload_url:
            raise SystemExit(
                f"{fname} 体积 {len(raw)} 字节超过 LoopForge MySQL 产物上限（64 KB），且未配置 Vernal 候选回传地址"
            )
        digest = hashlib.sha256(raw).hexdigest()
        urls = [upload_url]
        if Path("/.dockerenv").exists() and "://127.0.0.1:" in upload_url:
            urls.append(upload_url.replace("://127.0.0.1:", "://vernal:", 1))
        elif Path("/.dockerenv").exists() and "://localhost:" in upload_url:
            urls.append(upload_url.replace("://localhost:", "://vernal:", 1))
        reply = None
        last_exc = None
        for target_url in urls:
            req = urllib.request.Request(
                target_url,
                data=raw,
                method="POST",
                headers={"Content-Type": "application/json", "X-Vernal-Artifact": fname},
            )
            try:
                with opener.open(req, timeout=60) as resp:
                    reply = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:
                last_exc = exc
        if reply is None:
            raise SystemExit(f"{fname} 回传 Vernal 网关失败：{last_exc}")
        if reply.get("vernal_ref") != digest or reply.get("sha256") != digest:
            raise SystemExit(f"{fname} 回传 Vernal 校验不一致")
        stub = {"vernal_candidate_ref": digest, "sha256": digest, "bytes": len(raw)}
        fp.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
        print(f"{fname} 体积较大（{len(raw)} 字节），已安全上传至 Vernal 网关（ref={digest[:12]}）")


if __name__ == "__main__":
    write_review_context(find_impact_callers)
    spill_large_candidates()
