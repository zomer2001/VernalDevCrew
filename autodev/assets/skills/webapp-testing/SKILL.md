---
name: webapp-testing
description: 本地 Web 应用与静态页面自动化冒烟测试技能。用于在无头环境验证 HTTP 服务启动、静态资源 MIME 类型、DOM 挂载状态及浏览器交互逻辑。
---

# Web Application Smoke & Integration Testing

交付或审查 Web 前端任务时，不能只检查文件是否存在，必须进行可重复的自动化验证：

1. **HTTP 入口与模块加载校验**：
   - 若前端使用 ES Module（`.mjs` / `type="module"`），切勿依赖 `file://` 协议测试；应通过脚本临时拉起本地静态服务（如 `python3 -m http.server 0` 或 Node `http.createServer`）请求入口 HTML 及引用的 JS/CSS，断言状态码为 `200` 且 `Content-Type` 正确，测试完毕立即关闭端口。
2. **核心状态机与 UI 逻辑解耦单测**：
   - 将视图渲染状态计算、输入事件映射等逻辑抽离为可导入的纯模块，使用 `node --test` 或 `vitest`/`pytest` 覆盖关键交互流（初始化、操作反馈、异常/胜负弹窗、重开状态清零）。
3. **浏览器级真实渲染校验（可用时）**：
   - 当环境中挂载了 Playwright MCP 或本机存在无头浏览器时，实际导航至本地页面，断言首屏关键元素可见、点击核心按钮无 `console.error` 或未捕获异常（`pageerror`）。
   - 若当前沙箱无图形浏览器二进制，脚本应优雅降级输出明确说明，并以 Node/Python 语法检查（`node --check`）加 DOM/HTTP 冒烟测试作为必过门禁。
