"""WP-5 测试：多语言符号抽取、Personalized PageRank 种子排序、Token 预算截断、增量缓存、Tree-sitter 降级、Code Wiki 与问代码库。"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from autodev.assets import builtin_mcp_templates
from autodev.codeintel import build_repo_map, extract_file_symbols, find_impact_callers
from autodev.db import Task
from autodev.engine import Engine, init_workspace
from autodev.harnesses.mock import MockHarness
from autodev.models import TaskStatus
from autodev.prompts import build_coder_prompt, build_review_prompt
from autodev.server.app import create_app


def _offline_tp() -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))


def test_multilang_symbol_extraction_and_tree_sitter_fallback(tmp_path: Path) -> None:
    py_file = tmp_path / "svc.py"
    py_file.write_text(
        "class OrderService:\n"
        "    def create_order(self, sku: str, qty: int) -> dict:\n"
        "        return validate_sku(sku)\n",
        "utf-8",
    )
    ts_file = tmp_path / "widget.ts"
    ts_file.write_text(
        "export interface OrderCardProps { id: string }\n"
        "export async function fetchOrderDetail(orderId: string) { return orderId; }\n",
        "utf-8",
    )
    go_file = tmp_path / "store.go"
    go_file.write_text(
        "package store\n"
        "type OrderRepo struct {}\n"
        "func (r *OrderRepo) SaveOrder(id string) error { return nil }\n",
        "utf-8",
    )
    java_file = tmp_path / "Validator.java"
    java_file.write_text(
        "public class Validator {\n"
        "    public static boolean checkLimit(int amount) { return amount > 0; }\n"
        "}\n",
        "utf-8",
    )

    py_res = extract_file_symbols(py_file, allow_tree_sitter=False)
    assert py_res["backend"] == "ast+regex"
    assert {d["name"] for d in py_res["defs"]} >= {"OrderService", "create_order"}
    assert "validate_sku" in py_res["refs"]

    ts_res = extract_file_symbols(ts_file, allow_tree_sitter=False)
    assert {d["name"] for d in ts_res["defs"]} >= {"OrderCardProps", "fetchOrderDetail"}

    go_res = extract_file_symbols(go_file, allow_tree_sitter=False)
    assert {d["name"] for d in go_res["defs"]} >= {"OrderRepo", "SaveOrder"}

    java_res = extract_file_symbols(java_file, allow_tree_sitter=False)
    assert {d["name"] for d in java_res["defs"]} >= {"Validator", "checkLimit"}

    # Serena MCP 模板已注册且默认不强绑角色
    mcp_ids = {m["id"] for m in builtin_mcp_templates()}
    assert "vernal-serena-mcp" in mcp_ids


def test_pagerank_seeds_budget_truncation_incremental_cache_wiki_and_ask(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / "ci-repo", name="CodeIntelDemo", collaboration_mode="files")
    (ws.root / "billing").mkdir(parents=True)
    (ws.root / "auth").mkdir(parents=True)
    (ws.root / "gateway").mkdir(parents=True)

    (ws.root / "billing" / "invoice.py").write_text(
        "class InvoiceEngine:\n"
        "    def compute_tax(self, subtotal: float) -> float:\n"
        "        return subtotal * 0.13\n",
        "utf-8",
    )
    (ws.root / "gateway" / "checkout.py").write_text(
        "from billing.invoice import InvoiceEngine\n\n"
        "def submit_checkout(amount: float) -> float:\n"
        "    eng = InvoiceEngine()\n"
        "    return eng.compute_tax(amount)\n",
        "utf-8",
    )
    (ws.root / "auth" / "tokens.py").write_text(
        "def verify_jwt_token(raw: str) -> bool:\n"
        "    return bool(raw)\n",
        "utf-8",
    )

    # 1. 首次构建：全部 miss，写入 .autodev/codeintel/repomap_cache.json；且种子指向 InvoiceEngine 时 billing 排最前
    rmap1 = build_repo_map(
        ws.root,
        max_tokens=2000,
        seed_paths=["billing/"],
        seed_identifiers={"InvoiceEngine", "compute_tax"},
        allow_tree_sitter=False,
    )
    assert rmap1.cache_misses == 3 and rmap1.cache_hits == 0
    assert rmap1.ranked_files[0] == "billing/invoice.py"

    # 2. 二次构建：未改动文件全部命中缓存；修改 1 个文件仅触发 1 次增量 miss
    rmap2 = build_repo_map(ws.root, max_tokens=2000, seed_paths=["auth/"], allow_tree_sitter=False)
    assert rmap2.cache_hits == 3 and rmap2.cache_misses == 0
    assert rmap2.ranked_files[0] == "auth/tokens.py"

    (ws.root / "auth" / "tokens.py").write_text(
        "def verify_jwt_token(raw: str) -> bool:\n    return len(raw) > 3\n",
        "utf-8",
    )
    rmap3 = build_repo_map(ws.root, max_tokens=2000, allow_tree_sitter=False)
    assert rmap3.cache_hits == 2 and rmap3.cache_misses == 1

    # 3. 极小预算截断：输出严格受控在预算范围内
    tiny = build_repo_map(ws.root, max_tokens=40, allow_tree_sitter=False, persist_cache=False)
    assert len(tiny.text) <= 200

    # 4. 变更影响面：修改 billing/invoice.py 的 compute_tax 能识别 gateway/checkout.py 调用方
    diff_sample = (
        "diff --git a/billing/invoice.py b/billing/invoice.py\n"
        "--- a/billing/invoice.py\n"
        "+++ b/billing/invoice.py\n"
        "@@ -1,3 +1,3 @@\n"
        " class InvoiceEngine:\n"
        "-    def compute_tax(self, subtotal: float) -> float:\n"
        "+    def compute_tax(self, subtotal: float, rate: float = 0.13) -> float:\n"
    )
    impacts = find_impact_callers(ws.root, diff_sample)
    assert any(it["symbol"] == "compute_tax" and "gateway/checkout.py" in it["callers"] for it in impacts)

    eng = Engine(ws, harness=MockHarness())
    try:
        dummy_task = Task(
            task_id="T-001",
            milestone_id="M1",
            squad="billing",
            module="billing",
            role="coder",
            title="扩展税率参数",
            description="更新 InvoiceEngine.compute_tax",
            acceptance_criteria=["支持自定义税率"],
            depends_on=[],
            doc_slice_ids=[],
            allowed_paths=["billing/"],
            verify_cmds=["pytest -q"],
            priority=10,
            status=TaskStatus.IN_PROGRESS,
            retry_count=0,
            max_retries=2,
            arbitration_count=0,
            assigned_slot=None,
            active_agent_label=None,
            loopforge_issue_id=None,
            loopforge_run_id=None,
            worktree_path=None,
            branch_name=None,
            base_sha=None,
            last_session_id=None,
            artifact_digest=None,
            candidate_commit_sha=None,
            bound_contract_hash=None,
            last_feedback=None,
            failure_reason=None,
            steer_note=None,
        )
        cp = build_coder_prompt(dummy_task, [], "", eng.settings, ws_root=ws.root)
        assert "Repo Map" in cp and "InvoiceEngine" in cp
        rp = build_review_prompt(dummy_task, [], "ok", diff_sample, ws_root=ws.root)
        assert "变更符号影响面" in rp and "gateway/checkout.py" in rp

        # 5. HTTP 路由：生成 Code Wiki（含逆向 ARCHITECTURE.md 初稿）与只读 Ask 问答
        client = TestClient(create_app(eng, proxy_transport=_offline_tp()))
        wiki_res = client.post("/vernal-api/wiki/generate", json={"actor": "arch"})
        assert wiki_res.status_code == 200
        gen_docs = wiki_res.json()["generated_docs"]
        assert "docs/wiki/INDEX.md" in gen_docs
        assert "docs/ARCHITECTURE.md" in gen_docs
        assert (ws.root / "docs" / "ARCHITECTURE.md").is_file()

        ask_res = client.post("/vernal-api/ask", json={"question": "compute_tax 在哪里被调用？"})
        assert ask_res.status_code == 200
        ask_data = ask_res.json()
        assert any(c["file"] == "gateway/checkout.py" for c in ask_data["citations"])
    finally:
        eng.db.close()
