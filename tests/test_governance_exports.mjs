import assert from "node:assert/strict";
import test from "node:test";
import { setActiveProjectId } from "../autodev/server/static/js/api.js";
import { initGovernanceModule } from "../autodev/server/static/js/governance.js";

test("trace downloads follow the current project when the existing panel is reused", async () => {
  const elements = new Map();
  const panel = {
    appendChild(host) { elements.set(host.id, host); },
  };
  const matrix = {};
  elements.set("view-council", panel);
  elements.set("trace-matrix-box", matrix);
  globalThis.CSS = { escape: String };
  globalThis.document = {
    getElementById: (id) => elements.get(id) || null,
    querySelector: (selector) => elements.get(selector.slice(1)) || null,
    createElement: () => ({
      style: {},
      set innerHTML(html) {
        this.links = [...html.matchAll(/data-trace-export="([^"]+)"/g)]
          .map((match) => ({ dataset: { traceExport: match[1] } }));
      },
      querySelectorAll() { return this.links; },
    }),
  };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ rows: [] }) });
  let hooks;
  initGovernanceModule({ registerViewHooks: (value) => { hooks = value; } });
  const open = async () => {
    hooks.onCouncilOpen();
    await new Promise((resolve) => setImmediate(resolve));
  };
  const assertProject = (host, project) => {
    assert.equal(host.links.length, 2);
    for (const link of host.links) {
      const url = new URL(link.href, "http://localhost");
      assert.equal(url.pathname, "/vernal-api/trace/export");
      assert.equal(url.searchParams.get("format"), link.dataset.traceExport);
      assert.equal(url.searchParams.get("project"), project);
    }
  };

  setActiveProjectId("first");
  await open();
  const host = elements.get("codeintel-trace-host");
  assertProject(host, "first");
  setActiveProjectId("第二个 & project");
  await open();
  assert.equal(elements.get("codeintel-trace-host"), host);
  assertProject(host, "第二个 & project");
  setActiveProjectId("");
  await open();
  assertProject(host, null);
});
