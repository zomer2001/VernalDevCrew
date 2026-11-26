import assert from "node:assert/strict";
import test from "node:test";
import { createTimeline } from "../autodev/server/static/js/timeline.js";

test("switching projects drops cached events and ignores a delayed response", async () => {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        addEventListener() {},
        querySelectorAll() { return []; },
        replaceChildren(option) { this.value = option.value; },
        add() {},
      });
    }
    return elements.get(id);
  };
  globalThis.CSS = { escape: String };
  globalThis.document = { getElementById: () => null, querySelector: element };
  globalThis.Option = class { constructor(label, value) { this.value = value; } };
  const response = (data) => ({ ok: true, json: async () => data });
  let releaseOldPage;
  let pageRequested;
  const waiting = new Promise((resolve) => { pageRequested = resolve; });
  globalThis.fetch = async (path) => {
    if (path.endsWith("/workspaces")) return response({ workspaces: ["first", "second"] });
    if (path.includes("workspace=first")) {
      pageRequested();
      return new Promise((resolve) => { releaseOldPage = resolve; });
    }
    return response({ events: [], next_before: null });
  };
  const event = (id, message) => ({ event_id: id, message, event_type: "agent.message" });
  const timeline = createTimeline(() => {});
  const list = element("#event-list");
  const old = timeline.start({ root: "/first", recent_events: [event(20, "old model")] });
  await waiting;
  await timeline.start({ root: "/second", recent_events: [event(1, "current model")] });
  assert.doesNotMatch(list.innerHTML, /old model/);
  releaseOldPage(response({ events: [event(21, "late reply")], next_before: null }));
  await old;
  assert.match(list.innerHTML, /current model/);
  assert.doesNotMatch(list.innerHTML, /old model|late reply/);
});
