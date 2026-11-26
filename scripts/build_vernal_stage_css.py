"""把旧版独立看板的组件样式 styles.css 生成为作用域限定在 #vernalStage 内的 vernal-stage.css，
避免与 LoopForge 原生全局样式（.view / body / :root 等）互相污染。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "autodev/server/static/styles.css"
DST = ROOT / "autodev/server/static/vernal-stage.css"
SCOPE = "#vernalStage"
SKIP_SELECTORS = {".view", ".view.active"}


def split_blocks(css: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    i, n = 0, len(css)
    while i < n:
        while i < n and css[i].isspace():
            i += 1
        if i >= n:
            break
        # 跳过注释
        if css.startswith("/*", i):
            j = css.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        brace = css.find("{", i)
        if brace < 0:
            break
        head = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        out.append((head, css[brace + 1 : j - 1]))
        i = j
    return out


def scope_selector(sel: str) -> str | None:
    parts = []
    for s in sel.split(","):
        s = s.strip()
        if not s or s in SKIP_SELECTORS:
            continue
        if s in (":root", "html", "body"):
            parts.append(SCOPE)
        elif s == "*":
            parts.append(f"{SCOPE}, {SCOPE} *")
        elif s.startswith("body "):
            parts.append(f"{SCOPE} {s[5:]}")
        elif s in ('html[data-theme="dark"]', 'html[data-theme="light"]'):
            parts.append(f"{s} {SCOPE}")
        elif s.startswith('html[data-theme="dark"] '):
            parts.append(f'html[data-theme="dark"] {SCOPE} {s[len("html[data-theme=\"dark\"] "):]}')
        elif s.startswith('html[data-theme="light"] '):
            parts.append(f'html[data-theme="light"] {SCOPE} {s[len("html[data-theme=\"light\"] "):]}')
        else:
            parts.append(f"{SCOPE} {s}")
    return ", ".join(dict.fromkeys(parts)) if parts else None


def transform(css: str) -> str:
    lines: list[str] = []
    for head, body in split_blocks(css):
        if head.startswith("@keyframes") or head.startswith("@font-face"):
            lines.append(f"{head} {{{body}}}")
        elif head.startswith("@media") or head.startswith("@supports"):
            inner = transform(body)
            lines.append(f"{head} {{\n{inner}\n}}")
        else:
            scoped = scope_selector(head)
            if scoped:
                body = body.replace("height: 100%;", "") if head in ("html, body", "body") else body
                lines.append(f"{scoped} {{{body}}}")
    return "\n".join(lines)


def main() -> None:
    css = SRC.read_text("utf-8")
    DST.write_text("/* 自动生成：scripts/build_vernal_stage_css.py，请勿手改 */\n" + transform(css) + "\n", "utf-8")
    print(f"written {DST}")


if __name__ == "__main__":
    main()
