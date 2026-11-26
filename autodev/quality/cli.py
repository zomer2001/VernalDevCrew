"""命令行入口：供本地或 CI Runner 沙箱执行质量门禁并输出 JSON（WP-4 / WP-3）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from autodev.quality.runner import run_quality


def install_standalone_quality_cli(dest_root: Path, *, python_bin: str | None = None) -> Path:
    """将独立零第三方依赖的质量门禁包与 `vernal-quality` 可执行入口安装至目标目录（与 Dockerfile.sandbox 同构）。"""
    pkg_src = Path(__file__).resolve().parents[1]
    lib_dir = dest_root / "lib" / "vernal-quality" / "autodev"
    bin_dir = dest_root / "bin"
    if lib_dir.exists():
        shutil.rmtree(lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    (lib_dir / "__init__.py").write_text('__version__ = "0.2.0"\n', "utf-8")
    shutil.copytree(
        pkg_src / "quality",
        lib_dir / "quality",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    semgrep_src = pkg_src / "assets" / "semgrep"
    if semgrep_src.is_dir():
        (lib_dir / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            semgrep_src,
            lib_dir / "assets" / "semgrep",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    py = shlex.quote(python_bin or sys.executable)
    lib_parent = shlex.quote(str(lib_dir.parent.resolve()))
    wrapper = bin_dir / "vernal-quality"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH={lib_parent}${{PYTHONPATH:+:$PYTHONPATH}} exec {py} -m autodev.quality.cli "$@"\n',
        "utf-8",
    )
    os.chmod(wrapper, 0o755)
    return wrapper


def main() -> int:
    parser = argparse.ArgumentParser(description="Vernal Quality Gate CLI")
    parser.add_argument("--stage", choices=("task", "milestone"), default="milestone")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--coverage-threshold", type=float, default=0.0)
    parser.add_argument("--out", default="")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()

    cfg = {"changed_line_coverage": args.coverage_threshold} if args.coverage_threshold > 0 else {}
    results = asyncio.run(
        run_quality(
            args.stage,
            cwd=Path(args.cwd).resolve(),
            changed_files=args.files,
            base_ref=args.base_ref,
            cfg=cfg,
        )
    )
    payload = {
        "stage": args.stage,
        "passed": not any(r.status == "FAILED" for r in results),
        "results": [r.model_dump() for r in results],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(encoded + "\n", "utf-8")
    else:
        print(encoded)
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
