"""测试文档解析、标题树切片、代码块围栏忽略与 catalog 输出。"""

from __future__ import annotations

import json
from pathlib import Path

from autodev.ingest import ingest_directory, write_catalog


def test_markdown_slicing_ignores_fenced_headings_and_packs(tmp_path: Path) -> None:
    doc = """# 系统架构

总体说明。

```python
# 这不是 Markdown 标题
def foo():
    pass
```

## 存储模块

""" + ("存储引擎设计说明段落。\n\n" * 60) + """
## 接口模块

接口定义说明。
"""
    (tmp_path / "arch.md").write_text(doc, "utf-8")
    (tmp_path / "service.proto").write_text('syntax = "proto3";\nmessage Ping { string id = 1; }\n', "utf-8")
    (tmp_path / ".hidden.md").write_text("# ignored\n", "utf-8")

    slices = ingest_directory(tmp_path, max_chars=500, min_chars=120)
    assert len(slices) >= 3
    assert all(not s.source_file.startswith(".") for s in slices)
    assert all(s.char_count <= 500 for s in slices)
    assert any("service.proto" == s.source_file for s in slices)
    assert not any("这不是 Markdown 标题" in s.heading_path for s in slices)

    catalog_path = tmp_path / "doc_catalog.json"
    write_catalog(slices, catalog_path)
    data = json.loads(catalog_path.read_text("utf-8"))
    assert len(data) == len(slices)
    assert "content" not in data[0]
    assert data[0]["slice_id"] == "DOC01-S01"
