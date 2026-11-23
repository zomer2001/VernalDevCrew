# Mini-KV 本地键值存储引擎（试点示例文档）

## 1. 目标与背景

构建一个轻量、零第三方依赖的本地键值存储库与命令行工具（使用 Python 3 标准库 + `unittest`），用于验证 AutoDev Hub 从架构文档自动拆解、契约先行、并行开发到集成测试的完整闭环。

## 2. 系统模块划分

系统分为三个职责清晰的模块：

1. **contracts/**（公共接口契约，冻结后只读）
   - 定义异常：`KeyNotFoundError(KeyError)`、`InvalidKeyError(ValueError)`
   - 定义数据结构：`Entry(key: str, value: str, ttl_sec: float | None = None)`
   - 定义抽象存储接口 `KVStore`：
     - `set(key: str, value: str, ttl_sec: float | None = None) -> None`
     - `get(key: str) -> str`（不存在或已过期抛出 `KeyNotFoundError`）
     - `delete(key: str) -> bool`（返回是否存在并被删除）
     - `keys(prefix: str = "") -> list[str]`（按字典序返回未过期的键列表）

2. **storage/**（存储引擎实现）
   - `MemoryStore(KVStore)`：线程安全（使用 `threading.RLock`）的内存键值存储，支持 TTL 过期自动惰性清理。
   - `JsonFileStore(KVStore)`：基于 `MemoryStore` 增加 JSON 文件持久化（原子写临时文件再 `os.replace`），重启后可恢复未过期数据。

3. **service/**（业务服务与命令解析）
   - `CommandProcessor`：接收文本命令（`SET k v [ttl]`、`GET k`、`DEL k`、`KEYS [prefix]`、`STATS`）并调用 `KVStore` 执行，返回结构化字符串响应。
   - 遇到空键、非法参数时给出明确错误提示，不崩溃。

## 3. 质量与测试要求

- 每个模块必须具备独立的 `unittest` 单元测试，覆盖正常路径、空键/非法参数、TTL 过期、并发读写及文件重启恢复。
- 所有测试在无网络环境下通过 `python3 -m unittest discover -s tests -t . -v` 执行且 100% 通过。
