# Order-Hub 多业务域微型后端架构（多研发组 Goal 试跑示例）

## 1. 目标愿景

构建一个零外部依赖（Python 3 标准库 + `unittest`）的多业务域订单后端核心引擎，包含 **用户域、商品库存域、订单域、统一应用网关** 四个子系统，用于演练 AutoDev Hub 的「PM 规格补全 → 研发总监划分多研发组与里程碑路线图 → M1 冻结契约 → M2 多研发组并行开发 + 网关装配收口 → QA 全链路回归」完整闭环。

## 2. 系统域划分

1. **公共契约层（`contracts/`，冻结后只读）**
   - 领域数据类：`User`、`Product`、`OrderItem`、`Order`（状态流转：`CREATED -> PAID -> CANCELLED`）。
   - 领域异常体系：`DomainError` 基类，以及 `NotFoundError`、`InsufficientStockError`、`InvalidStateError`、`ValidationError`。
   - 各域仓储/服务抽象接口协议：`UserRepository`、`InventoryRepository`、`OrderRepository`。

2. **用户域（`modules/user/`）**
   - 用户注册、查询、禁用/启用；邮箱唯一性校验与空值校验。

3. **商品库存域（`modules/inventory/`）**
   - 商品上架（含单价与库存）、线程安全库存扣减（`reserve`）与回滚释放（`release`），库存不足抛 `InsufficientStockError`。

4. **订单域（`modules/order/`）**
   - 创建订单（校验用户存在、批量预占库存并计算总价，任一商品库存不足时自动回滚已预占库存）、支付订单（仅 `CREATED` 允许支付）、取消订单（`CREATED` 取消并自动释放库存，`PAID` 不允许直接取消）。

5. **统一应用网关（`gateway/`）**
   - 将用户域、商品库存域、订单域装配为统一外观 `OrderHubApp`，对外提供结构化请求/响应（含统一错误码映射），并附带跨模块端到端业务流测试。

## 3. 质量门禁要求

- 仅使用 Python 3 标准库 + `unittest`。
- 各业务组仅在各自目录及专属单测文件内开发，公共入口挂载由网关装配任务收口完成。
- 仓库根目录执行 `python3 -m unittest discover -s tests -t . -v` 全部通过。
