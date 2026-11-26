"""规范化研发文档中心路由（WP-1：/vernal-api/docs* 树查询、多版本编辑、Diff 对比、批注与 Word/ZIP 导出）。"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from autodev.docs_center.exporter import markdown_to_docx_bytes
from autodev.docs_center.service import (
    add_doc_comment,
    diff_doc_versions,
    generate_detailed_design,
    get_doc,
    list_doc_versions,
    list_docs,
    save_doc_version,
    sync_standard_docs_from_artifacts,
)
from autodev.engine import PlanningError
from autodev.server.deps import effective_actor, get_ctx

router = APIRouter()


class DocSaveRequest(BaseModel):
    content: str
    note: str = ""
    actor: str = "owner"


class DocCommentRequest(BaseModel):
    section: str = ""
    body: str
    actor: str = "owner"


class DetailedDesignGenerateRequest(BaseModel):
    milestone_id: str = "M1"
    squad_id: str = ""


@router.get("/vernal-api/docs")
async def api_list_docs(request: Request, sync: bool = True) -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    if sync:
        sync_standard_docs_from_artifacts(eng)
    return list_docs(eng)


@router.post("/vernal-api/docs/design/generate")
async def api_generate_detailed_design(request: Request, req: DetailedDesignGenerateRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    squads = eng.db.list_squads()
    targets = [req.squad_id.strip()] if req.squad_id.strip() else [s["squad_id"] for s in squads]
    if not targets:
        targets = ["core"]
    generated: list[str] = []
    try:
        for sq_id in targets:
            await generate_detailed_design(eng, req.milestone_id, sq_id)
            generated.append(f"docs/design/{sq_id}.md")
    except PlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "milestone_id": req.milestone_id, "generated": generated}


@router.get("/vernal-api/docs/export")
async def api_export_docs(
    request: Request,
    kinds: str = "SRS,ADD,DETAILED_DESIGN,TEST_REPORT",
    format: str = "docx",
    lab_name: str = "Vernal 软件工程实验室",
    classification: str = "内部公开",
) -> Response:
    eng = get_ctx(request).active_slot(request).engine
    sync_standard_docs_from_artifacts(eng)
    all_docs = list_docs(eng)
    wanted = {k.strip().upper() for k in kinds.split(",") if k.strip()}
    matched = [d for d in all_docs if not wanted or d["kind"] in wanted]
    if not matched:
        raise HTTPException(status_code=404, detail="当前没有匹配所选类型的规范文档可供导出")

    fmt = format.strip().lower()
    if fmt == "docx" and len(matched) == 1:
        doc_obj = get_doc(eng, matched[0]["path"])
        data = markdown_to_docx_bytes(
            doc_obj["content"],
            lab_name=lab_name,
            classification=classification,
            version_label=f"v{doc_obj['version']}",
        )
        stem = matched[0]["path"].rsplit("/", 1)[-1].replace(".md", "")
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{stem}.docx"'},
        )

    if fmt == "md" and len(matched) == 1:
        doc_obj = get_doc(eng, matched[0]["path"])
        stem = matched[0]["path"].rsplit("/", 1)[-1]
        return Response(
            content=doc_obj["content"].encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}"'},
        )

    # 多份文档或显式 zip：打包为 ZIP（包含 .md 与 .docx）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in matched:
            doc_obj = get_doc(eng, item["path"])
            if fmt in ("md", "zip"):
                zf.writestr(item["path"], doc_obj["content"])
            if fmt in ("docx", "zip"):
                docx_path = item["path"][:-3] + ".docx" if item["path"].endswith(".md") else item["path"] + ".docx"
                zf.writestr(
                    docx_path,
                    markdown_to_docx_bytes(
                        doc_obj["content"],
                        lab_name=lab_name,
                        classification=classification,
                        version_label=f"v{doc_obj['version']}",
                    ),
                )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vernal-spec-docs.zip"'},
    )


@router.get("/vernal-api/docs/{doc_path:path}/versions")
async def api_doc_versions(doc_path: str, request: Request) -> list[dict[str, Any]]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return list_doc_versions(eng, doc_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/vernal-api/docs/{doc_path:path}/diff")
async def api_doc_diff(doc_path: str, request: Request, from_ver: int = 1, to_ver: int = 2) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return diff_doc_versions(eng, doc_path, from_ver, to_ver)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/vernal-api/docs/{doc_path:path}/comments")
async def api_doc_add_comment(doc_path: str, request: Request, req: DocCommentRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    actor = effective_actor(req.actor)
    try:
        return add_doc_comment(eng, doc_path, section=req.section, body=req.body, author=actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/vernal-api/docs/{doc_path:path}")
async def api_get_doc(doc_path: str, request: Request, version: int | None = None) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return get_doc(eng, doc_path, version=version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/vernal-api/docs/{doc_path:path}")
async def api_put_doc(doc_path: str, request: Request, req: DocSaveRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    actor = effective_actor(req.actor)
    try:
        return save_doc_version(eng, doc_path, req.content, author=actor, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
