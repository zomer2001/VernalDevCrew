"""Human communication rules. Transport and workspace setup belong to SOP scaffolding."""
import re

COMMUNICATION_STYLE = """表达约定：
用中文先说明结论，再说明依据。首句不超过 40 字。讲清用户行为、范围、接口和验收结果。
产品经理说明范围取舍，架构师说明边界和代价，质量负责人说明可测性，总监说明决定与理由。
对外文档、任务标题和交接便签不包含内部派单标识或文件搬运协议；诊断信息由平台另行记录。
反例：建议工作项 issue-123 在本工位独立完成文件交接模式和平台集成验证。
正例：首版提供上传文档和下载结果，在线编辑留到后续版本。
交接便签必须包含 delivered（我交付了什么）、integration（下游怎么接）、
boundaries（边界与注意事项）、verification（验证方式与真实结果）。
未运行测试必须明确写未运行，不得把格式检查称为功能验证通过。
"""

_INTERNAL = re.compile(r"\bissue-[A-Za-z0-9_-]+|FILE_DELIVERY_STAGES|\.vernal-baseline\.json|vernal-files\.json|submit_gate_result|本工位独立完成|文件交接模式|平台集成验证")


def validate_public_text(text: str) -> None:
    if _INTERNAL.search(text):
        raise ValueError("对外文档或交接便签包含内部执行协议，请改为功能、接口与验证结果说明")
