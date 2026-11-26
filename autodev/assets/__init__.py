"""Vernal 内置研发资产库加载器：自动扫描 `autodev/assets/skills/*/SKILL.md` 及预置 MCP 目录。

用户后续只需将任意 GitHub 开源 Skill 文件夹放入 `autodev/assets/skills/<skill-id>/`，
切换或绑定项目时即会自动同步注入到底层 LoopForge 资产中心。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ASSETS_ROOT = Path(__file__).parent
SKILLS_DIR = ASSETS_ROOT / "skills"


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :].strip()
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def load_builtin_skills() -> list[dict[str, Any]]:
    """扫描 `autodev/assets/skills/*/SKILL.md`，返回标准化 Skill 清单。"""
    if not SKILLS_DIR.is_dir():
        return []
    skills: list[dict[str, Any]] = []
    for sub in sorted(SKILLS_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        raw = skill_md.read_text("utf-8")
        meta, body = _parse_frontmatter(raw)
        slug = re.sub(r"[^0-9A-Za-z._\-]+", "-", meta.get("name") or sub.name).strip("-").lower() or sub.name
        extra_files: dict[str, str] = {}
        for fp in sorted(sub.rglob("*")):
            if not fp.is_file() or fp.name == "SKILL.md" or fp.name.startswith("."):
                continue
            rel = fp.relative_to(sub).as_posix()
            try:
                extra_files[rel] = fp.read_text("utf-8")
            except UnicodeDecodeError:
                continue
        skills.append(
            {
                "slug": slug,
                "name": meta.get("name") or slug,
                "description": meta.get("description") or f"Vernal 内置研发技能 {slug}",
                "content": body or raw,
                "files": extra_files,
            }
        )
    return skills


def builtin_mcp_templates() -> list[dict[str, Any]]:
    """预置注册到 LoopForge MCP 目录的高频开源 MCP 插件（供界面按需勾选挂载，避免离线环境强依赖报错）。"""
    return [
        {
            "id": "vernal-playwright-mcp",
            "name": "Playwright 无头浏览器验收 MCP",
            "description": "开源 microsoft/playwright-mcp：驱动真实无头浏览器打开页面、点击交互、捕获控制台错误与截图（需联网或本机预装 Chromium）",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless"],
            "timeoutSec": 120,
        },
        {
            "id": "vernal-context7-mcp",
            "name": "Context7 实时官方文档库 MCP",
            "description": "开源 upstash/context7：实时检索第三方框架与 SDK 最新版本官方文档，消除过期 API 幻觉（联网环境可用）",
            "transport": "http",
            "url": "https://mcp.context7.com/mcp",
            "timeoutSec": 60,
        },
        {
            "id": "vernal-serena-mcp",
            "name": "Serena 符号级代码导航 MCP",
            "description": "开源 oraios/serena（MIT）：基于 LSP 提供跨文件符号定义跳转、引用查找与结构化重构（默认不强绑角色，按需挂载）",
            "transport": "stdio",
            "command": "serena-mcp-server",
            "args": ["--context", "ide-assistant"],
            "timeoutSec": 90,
        },
    ]
