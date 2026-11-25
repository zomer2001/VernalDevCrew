"""自研 AST 假测试检测门禁（WP-4：拦截空壳测试、恒真断言、无断言测试、整文件跳过与吞异常）。"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

from autodev.quality.model import Finding, QualityResult


def _is_python_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    parts = Path(rel_path).parts
    return rel_path.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py") or "tests" in parts
    )


def _is_trivial_truthy(expr: ast.AST) -> bool:
    if isinstance(expr, ast.Constant):
        return bool(expr.value) is True
    if isinstance(expr, ast.Compare) and len(expr.ops) == 1 and len(expr.comparators) == 1:
        left, right = expr.left, expr.comparators[0]
        if isinstance(expr.ops[0], (ast.Eq, ast.Is)) and isinstance(left, ast.Constant) and isinstance(right, ast.Constant):
            return left.value == right.value
    return False


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return ""


def _scan_py_test_ast(rel_path: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        findings.append(
            Finding(
                tool="test_integrity",
                rule="syntax-error",
                severity="blocker",
                file=rel_path,
                line=exc.lineno or 1,
                message=f"测试文件存在语法错误：{exc.msg}",
            )
        )
        return findings

    # 1. 检测整文件级 skip（pytestmark = pytest.mark.skip / module-level pytest.skip）
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "pytestmark" in targets and "skip" in ast.dump(stmt.value):
                findings.append(
                    Finding(
                        tool="test_integrity",
                        rule="whole-file-skip",
                        severity="blocker",
                        file=rel_path,
                        line=stmt.lineno,
                        message="禁止使用 pytestmark 在整文件级别跳过测试",
                    )
                )
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            cname = _call_name(stmt.value)
            if cname in ("pytest.skip", "unittest.skip"):
                findings.append(
                    Finding(
                        tool="test_integrity",
                        rule="whole-file-skip",
                        severity="blocker",
                        file=rel_path,
                        line=stmt.lineno,
                        message=f"禁止在模块顶层调用 {cname} 跳过整份测试文件",
                    )
                )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue

        # 过滤掉纯 docstring 语句后的真实可执行体
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]

        # 2. 空测试函数（仅 pass 或 ...）
        if not body or all(
            isinstance(s, ast.Pass)
            or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis)
            for s in body
        ):
            findings.append(
                Finding(
                    tool="test_integrity",
                    rule="empty-test",
                    severity="blocker",
                    file=rel_path,
                    line=node.lineno,
                    message=f"测试函数 `{node.name}` 为空壳实现（仅含 pass/...），未执行任何真实校验",
                )
            )
            continue

        meaningful_asserts = 0
        trivial_lines: list[int] = []

        for sub in ast.walk(node):
            # 3. 恒真断言检测（assert True / assert 1 == 1）
            if isinstance(sub, ast.Assert):
                if _is_trivial_truthy(sub.test):
                    trivial_lines.append(sub.lineno)
                else:
                    meaningful_asserts += 1
            elif isinstance(sub, ast.Call):
                cname = _call_name(sub)
                attr_tail = cname.rsplit(".", 1)[-1]
                if (
                    cname in ("pytest.raises", "pytest.fail", "unittest.TestCase.assertRaises")
                    or attr_tail.startswith("assert")
                    or attr_tail in ("raises", "fail")
                ):
                    meaningful_asserts += 1
            # 5. 测试内吞异常检测（except ...: pass）
            elif isinstance(sub, ast.ExceptHandler):
                h_body = sub.body
                if h_body and all(isinstance(s, ast.Pass) for s in h_body):
                    findings.append(
                        Finding(
                            tool="test_integrity",
                            rule="swallowed-exception",
                            severity="high",
                            file=rel_path,
                            line=sub.lineno,
                            message=f"测试函数 `{node.name}` 捕获异常后直接 pass 吞掉错误，会掩盖真实故障",
                        )
                    )

        for ln in trivial_lines:
            findings.append(
                Finding(
                    tool="test_integrity",
                    rule="trivial-assert",
                    severity="blocker",
                    file=rel_path,
                    line=ln,
                    message=f"测试函数 `{node.name}` 包含恒真占位断言（如 assert True / 1 == 1）",
                )
            )

        # 4. 无任何有效断言
        if meaningful_asserts == 0 and not trivial_lines:
            # 若函数显式装饰了 skip/xfail 则不重复报无断言，否则拦截
            decs = [ast.dump(d) for d in node.decorator_list]
            if not any("skip" in d or "xfail" in d for d in decs):
                findings.append(
                    Finding(
                        tool="test_integrity",
                        rule="missing-assertion",
                        severity="high",
                        file=rel_path,
                        line=node.lineno,
                        message=f"测试函数 `{node.name}` 未包含任何有效断言（assert / pytest.raises / mock.assert_*）",
                    )
                )

    return findings


async def run(
    cwd: Path,
    changed_files: list[str],
    base_ref: str = "",
    cfg: dict[str, Any] | None = None,
) -> QualityResult:
    t0 = time.monotonic()
    candidates = [f for f in changed_files if _is_python_test_file(f)]
    if not candidates and not changed_files:
        candidates = [
            str(p.relative_to(cwd)).replace("\\", "/")
            for p in cwd.rglob("*.py")
            if ".venv" not in p.parts and ".git" not in p.parts and _is_python_test_file(str(p.relative_to(cwd)))
        ]
    if not candidates:
        return QualityResult(
            gate="test_integrity",
            status="PASSED",
            metrics={"checked_test_files": 0.0},
            duration_sec=round(time.monotonic() - t0, 3),
        )

    all_findings: list[Finding] = []
    checked = 0
    for rel in candidates:
        fp = cwd / rel
        if not fp.is_file():
            continue
        checked += 1
        txt = fp.read_text("utf-8", errors="replace")
        all_findings.extend(_scan_py_test_ast(rel, txt))

    status = "FAILED" if all_findings else "PASSED"
    return QualityResult(
        gate="test_integrity",
        status=status,
        metrics={"checked_test_files": float(checked), "issues": float(len(all_findings))},
        findings=all_findings,
        duration_sec=round(time.monotonic() - t0, 3),
    )
