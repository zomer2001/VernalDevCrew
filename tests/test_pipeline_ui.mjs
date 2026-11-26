import assert from "node:assert/strict";
import test from "node:test";
import { describeGateState, renderPendingGateBanner } from "../autodev/server/static/js/pipeline.js";

function makePending(recOverrides = {}) {
  return {
    gate: { id: "srs_approval", name: "功能规格与宪章批准" },
    rec: {
      record_id: 5,
      round: 5,
      scope_type: "project",
      scope_id: "project",
      status: "PENDING",
      target_version: 3,
      target_sha: "abc123",
      content_current: true,
      ready_for_decision: false,
      can_approve: false,
      can_waive: false,
      blockers: [],
      ...recOverrides,
    },
  };
}

test("R3 pipeline banner separates checklist running from document staleness", () => {
  const html = renderPendingGateBanner(makePending({
    blockers: [{ code: "CHECKLIST_RUNNING", message: "规格质量检查正在进行，完成后可审阅批准", feature_id: "001-initial" }],
  }));
  assert.match(html, /规格质量检查正在进行/);
  assert.doesNotMatch(html, /内容已变更/);
  assert.doesNotMatch(html, /重新生成/);
  assert.match(html, /data-gate-blocker-code="CHECKLIST_RUNNING"/);
  assert.match(html, /disabled data-gate-decide="pass"/);
  assert.match(html, /disabled data-gate-decide="waive"/);
});

test("R3 pipeline banner shows distinct actions for open questions, critical findings, drift, and permission denied", () => {
  const cases = [
    {
      code: "OPEN_QUESTIONS",
      message: "功能 001-initial 还有影响交付的关键问题未回答（Q-01）",
      expectText: /关键问题未回答/,
      expectBtn: /前往回答问题/,
      canApprove: false,
    },
    {
      code: "CHECKLIST_CRITICAL_FINDINGS",
      message: "规格质量检查存在 1 项严重问题",
      expectText: /严重问题/,
      expectBtn: /查看发现并修订/,
      canApprove: false,
    },
    {
      code: "FILE_MISSING_OR_DRIFTED",
      message: "文档文件缺失或存在未归档外部改动（specs/001-initial/spec.md）",
      expectText: /未归档外部改动/,
      expectBtn: /进入文档修订\/恢复/,
      canApprove: false,
      content_current: false,
      status: "STALE",
    },
    {
      code: "PERMISSION_DENIED",
      message: "当前版本材料已齐全，但需要 admin, architect 角色权限方可批准",
      expectText: /角色权限/,
      expectBtn: null,
      canApprove: false,
      ready_for_decision: true,
    },
  ];
  for (const c of cases) {
    const html = renderPendingGateBanner(makePending({
      status: c.status || "PENDING",
      content_current: c.content_current ?? true,
      ready_for_decision: c.ready_for_decision ?? false,
      can_approve: c.canApprove,
      can_waive: false,
      blockers: [{ code: c.code, message: c.message, feature_id: "001-initial" }],
    }));
    assert.match(html, c.expectText);
    if (c.expectBtn) assert.match(html, c.expectBtn);
    assert.match(html, /disabled data-gate-decide="pass"/);
  }
});

test("R3 pipeline banner enables approve when all preconditions and permissions pass", () => {
  const pending = makePending({
    content_current: true,
    ready_for_decision: true,
    can_approve: true,
    can_waive: false,
    blockers: [],
  });
  const info = describeGateState(pending.rec);
  assert.equal(info.code, "READY");
  assert.equal(info.canApprove, true);
  assert.equal(info.canWaive, false);
  const html = renderPendingGateBanner(pending);
  assert.doesNotMatch(html, /disabled data-gate-decide="pass"/);
  assert.match(html, /disabled data-gate-decide="waive"/);
});
