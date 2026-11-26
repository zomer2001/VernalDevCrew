"""需求追溯矩阵 HTTP 路由（WP-6：/vernal-api/trace/matrix 与 /vernal-api/trace/export）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from autodev.server.deps import get_ctx
from autodev.trace import build_trace_matrix, export_trace_matrix_csv, export_trace_matrix_docx

router = APIRouter()


def _matrix(engine):
    try:
        return build_trace_matrix(engine)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/vernal-api/trace/matrix")
async def api_get_trace_matrix(request: Request) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    return _matrix(eng)


@router.get("/vernal-api/trace/export")
async def api_export_trace_matrix(request: Request, format: str = "csv") -> Response:
    eng = get_ctx(request).active_slot(request).engine
    matrix = _matrix(eng)
    fmt = format.strip().lower()
    if fmt == "csv":
        csv_text = export_trace_matrix_csv(matrix)
        return Response(
            content=csv_text.encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="traceability-matrix.csv"'},
        )
    if fmt == "docx":
        docx_bytes = export_trace_matrix_docx(matrix, project_name=eng.ws.root.name)
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="traceability-matrix.docx"'},
        )
    raise HTTPException(status_code=422, detail=f"不支持的导出格式：{format}（支持 csv / docx）")
