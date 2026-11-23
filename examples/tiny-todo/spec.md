# Tiny-Todo 极简待办清单（超小试跑需求）

## 1. 目标

用 Python 3 标准库实现一个极简的内存待办清单模块，只包含 2 个小模块，用于快速验证自动规划、契约锁定、编码与单元测试闭环。

## 2. 模块划分

### 模块 A：`contracts/`（公共契约，冻结后只读）
- 文件 `contracts/todo.py`：
  - 定义异常 `TodoNotFoundError(KeyError)`
  - 定义数据类 `TodoItem(id: int, title: str, done: bool = False)`
  - 定义抽象基类 `TodoRepo`：
    - `add(title: str) -> TodoItem`（标题去首尾空格后为空时抛 `ValueError`）
    - `complete(item_id: int) -> TodoItem`（找不到抛出 `TodoNotFoundError`）
    - `list_items(only_pending: bool = False) -> list[TodoItem]`（按 `id` 升序返回）

### 模块 B：`todo/`（核心实现）
- 文件 `todo/memory_repo.py`：
  - 实现 `MemoryTodoRepo(TodoRepo)`，自增 `id` 从 `1` 开始。
  - 实现上述 3 个方法，保证边界行为完全符合契约。

## 3. 验收与测试要求

- 仅使用 Python 3 标准库 + `unittest`，无第三方依赖。
- 提供对应单元测试：`tests/test_contracts.py` 与 `tests/test_memory_repo.py`，覆盖新增、完成、按状态筛选、空标题报错、不存在 ID 报错。
- 在仓库根目录执行 `python3 -m unittest discover -s tests -t . -v` 全部通过。
