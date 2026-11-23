import { $, api } from "./api.js";
import { renderEvents } from "./roster.js";

export function createTimeline(onSelectTask) {
  const source = $("history-workspace");
  const status = $("history-status");
  const older = $("history-older");
  const records = new Map();
  const recent = new Map();
  const pageSize = 60;
  let current = "";
  let selected = "";
  let generation = 0;
  let nextBefore = null;
  let loading = false;
  let connected = false;
  let failure = "";

  function draw() {
    const live = selected === current;
    renderEvents([...records.values()].sort((a, b) => b.event_id - a.event_id), live ? onSelectTask : null);
    const mode = live ? (connected ? "实时更新" : "正在连接实时通道") : "历史工作区 · 只读";
    status.textContent = failure || (loading ? "正在读取记录…" : (records.size ? mode : mode + " · 暂无事件"));
    older.hidden = nextBefore === null;
    older.disabled = loading;
  }

  function remember(event) {
    recent.set(event.event_id, event);
    if (recent.size > pageSize) recent.delete(recent.keys().next().value);
  }

  async function readPage(before = null) {
    const token = ++generation;
    loading = true;
    failure = "";
    draw();
    const params = new URLSearchParams({workspace:selected, limit:pageSize});
    if (before !== null) params.set("before", before);
    try {
      const page = await api("/vernal-api/history/events?" + params);
      if (token !== generation) return;
      for (const ev of page.events) records.set(ev.event_id, ev);
      nextBefore = page.next_before;
    } catch (err) {
      if (token === generation) failure = "历史记录读取失败：" + err.message;
    } finally {
      if (token === generation) { loading = false; draw(); }
    }
  }

  function select() {
    selected = source.value;
    records.clear();
    nextBefore = null;
    if (selected === current) for (const ev of recent.values()) records.set(ev.event_id, ev);
    return readPage();
  }

  source.addEventListener("change", select);
  older.addEventListener("click", () => readPage(nextBefore));
  return {
    async start(state) {
      current = state.root.split("/").filter(Boolean).pop();
      selected = current;
      source.replaceChildren(new Option(current + "（当前）", current));
      for (const ev of state.recent_events.slice(-pageSize)) {
        remember(ev);
        records.set(ev.event_id, ev);
      }
      draw();
      try {
        const catalog = await api("/vernal-api/history/workspaces");
        for (const name of catalog.workspaces) {
          if (name !== current) source.add(new Option(name, name));
        }
        await select();
      } catch (err) {
        failure = "已显示当前项目。历史工作区列表读取失败，请确认 Vernal 已重启。";
        draw();
      }
    },
    append(ev) {
      remember(ev);
      if (selected === current) { records.set(ev.event_id, ev); draw(); }
    },
    setConnected(value) { connected = value; draw(); },
  };
}
