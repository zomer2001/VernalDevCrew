"""Vernal 同源反向代理网关：透明承载 LoopForge 原生前端与 API，同时无侵入注入 Vernal 风格与多智能体架构视图。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from autodev.loopforge.client import create_async_http_client, use_environment_proxy

OVERLAY_VERSION = "20261126p2v1"

HOP_BY_HOP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
        "set-cookie",
    }
)

HTML_INJECT_PREFIXES = ("/native-chat", "/chatbot")


def _should_inspect_html(path: str) -> bool:
    if path in ("", "/", "/index.html", "/login.html"):
        return True
    if any(path.startswith(p) for p in HTML_INJECT_PREFIXES) and (
        path.endswith("/") or path.endswith(".html") or "." not in path.rsplit("/", 1)[-1]
    ):
        return True
    return False


def transform_loopforge_html(path: str, html: str) -> str:
    """将 LoopForge 原生 HTML 改造为 Vernal 品牌与蓝图样式，并挂载 Vernal 扩展模块。"""
    css_tag = f'  <link rel="stylesheet" href="/vernal-static/vernal-overlay.css?v={OVERLAY_VERSION}">\n'
    js_tag = f'  <script type="module" src="/vernal-static/vernal-overlay.js?v={OVERLAY_VERSION}"></script>\n'

    # 1. 替换页面标题中的 LoopForge 品牌为 Vernal
    html = re.sub(
        r"<title>[^<]*LoopForge[^<]*</title>",
        lambda m: m.group(0).replace("LoopForge · Agent 编排平台", "Vernal · 多智能体架构研发平台").replace("LoopForge", "Vernal"),
        html,
        flags=re.IGNORECASE,
    )

    if path == "/login.html":
        html = html.replace("<h1>LoopForge</h1>", "<h1>Vernal</h1>")
        html = html.replace("登录你的交付工作台", "登录 Vernal · 多智能体架构研发平台")
        if "</head>" in html and "vernal-overlay.css" not in html:
            html = html.replace("</head>", css_tag + "</head>", 1)
        return html

    if path in ("", "/", "/index.html"):
        html = html.replace('aria-label="LoopForge 工作台"', 'aria-label="Vernal 研发助手"')
        html = html.replace("正在进入工作台…", "正在进入 Vernal 多智能体研发工作台…")
        if "</head>" in html and "vernal-overlay.css" not in html:
            html = html.replace("</head>", css_tag + "</head>", 1)
        if "</body>" in html and "vernal-overlay.js" not in html:
            html = html.replace("</body>", js_tag + "</body>", 1)
        return html

    # iframe 内的 AI 研发助手页面（/native-chat/* 或 /chatbot/*）注入主题样式与轻量品牌文案同步
    brand_tag = f'  <script src="/vernal-static/vernal-brand.js?v={OVERLAY_VERSION}" defer></script>\n'
    if "</head>" in html and "vernal-overlay.css" not in html:
        html = html.replace("</head>", css_tag + "</head>", 1)
    if "vernal-brand.js" not in html:
        if "</body>" in html:
            html = html.replace("</body>", brand_tag + "</body>", 1)
        elif "</head>" in html:
            html = html.replace("</head>", brand_tag + "</head>", 1)
    return html


def _rewrite_origin_header(value: str, upstream_origin: str) -> str:
    if not value:
        return value
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return upstream_origin
    if parts.path or parts.query or parts.fragment:
        suffix = parts.path
        if parts.query:
            suffix += f"?{parts.query}"
        if parts.fragment:
            suffix += f"#{parts.fragment}"
        return upstream_origin.rstrip("/") + suffix
    return upstream_origin


def _offline_fallback_html(upstream_url: str, err: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Vernal · 等待执行底座启动</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      font-family: Inter, -apple-system, "PingFang SC", sans-serif;
      background-color: #f8fafc;
      background-image: radial-gradient(#cbd5e1 1px, transparent 1px);
      background-size: 20px 20px;
      color: #0f172a;
    }}
    .card {{
      max-width: 560px; background: #fff; border: 1px solid #e2e8f0;
      border-top: 4px solid #1d4ed8; border-radius: 14px; padding: 28px 32px;
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
    }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    p {{ margin: 8px 0; color: #475569; line-height: 1.6; font-size: 14px; }}
    code, pre {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #f1f5f9; border-radius: 8px; padding: 2px 6px; font-size: 13px;
    }}
    pre {{ padding: 12px 14px; overflow-x: auto; }}
    .actions {{ margin-top: 18px; display: flex; gap: 10px; }}
    button {{
      border: 1px solid #1d4ed8; background: #1d4ed8; color: #fff;
      border-radius: 8px; padding: 8px 16px; font-weight: 600; cursor: pointer;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Vernal 正在等待 LoopForge 执行底座就绪</h1>
    <p>当前无法连接到 LoopForge 执行底座 <code>{upstream_url}</code>（{err}）。</p>
    <p>请在终端启动容器栈后再刷新本页：</p>
    <pre>./vernal.sh</pre>
    <div class="actions">
      <a href="/">重新连接</a>
    </div>
  </div>
</body>
</html>"""


class LoopForgeProxy:
    def __init__(self, upstream_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.upstream_url = upstream_url.rstrip("/")
        parts = urlsplit(self.upstream_url)
        self.upstream_origin = f"{parts.scheme}://{parts.netloc}"
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        if transport is not None or not use_environment_proxy(self.upstream_url):
            self._client = self._ensure_client()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_http_client(
                self.upstream_url,
                transport=self._transport,
                base_url=self.upstream_url,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def forward(self, request: Request) -> Response:
        path = request.url.path or "/"
        query = request.url.query
        target = f"{path}?{query}" if query else path
        inspect_html = request.method == "GET" and _should_inspect_html(path)

        req_headers: dict[str, str] = {}
        client_origin = request.headers.get("origin", "")
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in HOP_BY_HOP_REQUEST_HEADERS:
                continue
            if inspect_html and kl == "accept-encoding":
                continue
            if kl == "origin":
                req_headers[k] = self.upstream_origin
            elif kl == "referer":
                req_headers[k] = _rewrite_origin_header(v, self.upstream_origin)
            else:
                req_headers[k] = v

        body = await request.body() if request.method not in ("GET", "HEAD") else b""

        try:
            client = self._ensure_client()
            upstream_req = client.build_request(
                request.method,
                target,
                headers=req_headers,
                content=body if body else None,
            )
            if "cookie" not in request.headers:
                upstream_req.headers.pop("cookie", None)
            upstream_resp = await client.send(upstream_req, stream=True)
        except (httpx.HTTPError, RuntimeError) as exc:
            if path.startswith("/api/") or request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse(
                    status_code=502,
                    content={"error": f"无法连接 LoopForge 执行底座 ({self.upstream_url}): {exc}"},
                )
            return HTMLResponse(status_code=502, content=_offline_fallback_html(self.upstream_url, str(exc)))

        content_type = upstream_resp.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type.lower() or path.endswith("/stream")

        resp_headers: list[tuple[str, str]] = []
        for k, v in upstream_resp.headers.multi_items():
            kl = k.lower()
            if kl in HOP_BY_HOP_RESPONSE_HEADERS:
                continue
            if kl == "location" and v.startswith(self.upstream_origin):
                v = v[len(self.upstream_origin) :] or "/"
            elif kl == "access-control-allow-origin" and client_origin:
                v = client_origin
            resp_headers.append((k, v))

        set_cookies = upstream_resp.headers.get_list("set-cookie")

        if is_sse:
            async def _stream_iter() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream_resp.aiter_raw():
                        if chunk:
                            yield chunk
                finally:
                    await upstream_resp.aclose()

            streaming = StreamingResponse(
                _stream_iter(),
                status_code=upstream_resp.status_code,
                media_type=content_type or "text/event-stream",
            )
            for k, v in resp_headers:
                if k.lower() != "content-type":
                    streaming.headers.append(k, v)
            streaming.headers["Cache-Control"] = "no-cache"
            streaming.headers["X-Accel-Buffering"] = "no"
            for sc in set_cookies:
                streaming.headers.append("set-cookie", sc)
            return streaming

        try:
            raw = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()

        if inspect_html and "text/html" in content_type.lower() and upstream_resp.status_code < 400:
            text = raw.decode("utf-8", errors="replace")
            raw = transform_loopforge_html(path, text).encode("utf-8")

        response = Response(
            content=raw,
            status_code=upstream_resp.status_code,
            media_type=content_type or None,
        )
        for k, v in resp_headers:
            if k.lower() != "content-type":
                response.headers.append(k, v)
        if upstream_resp.status_code >= 400 and path.startswith("/api/runs/") and path.endswith("/telemetry"):
            body_preview = raw[:512].decode("utf-8", errors="ignore").lower()
            if "1054" in body_preview and "unknown column" in body_preview and "adapter" in body_preview:
                response.headers["X-Vernal-Upstream-Diagnostic"] = "upstream-schema-column-missing:run_telemetry.adapter"
        for sc in set_cookies:
            response.headers.append("set-cookie", sc)
        return response
