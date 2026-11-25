"""代码理解 HTTP 路由（WP-5：/vernal-api/codeintel/repomap、/vernal-api/wiki/generate、/vernal-api/ask）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from autodev.codeintel import ask_codebase, build_repo_map, generate_code_wiki
from autodev.server.deps import effective_actor, get_ctx

router = APIRouter()


class AskCodebaseRequest(BaseModel):
    question: str
    max_matches: int = 10


class GenerateWikiRequest(BaseModel):
    actor: str = "owner"
    seed_paths: list[str] = Field(default_factory=list)


@router.get("/vernal-api/codeintel/repomap")
async def api_get_repomap(request: Request, max_tokens: int = 2000, seed_path: str = "") -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    seeds = [seed_path.strip()] if seed_path.strip() else None
    res = build_repo_map(eng.ws.root, max_tokens=max(200, min(max_tokens, 8000)), seed_paths=seeds)
    return {
        "text": res.text,
        "ranked_files": res.ranked_files,
        "backend": res.backend,
        "cache_hits": res.cache_hits,
        "cache_misses": res.cache_misses,
    }


@router.post("/vernal-api/wiki/generate")
async def api_generate_wiki(request: Request, req: GenerateWikiRequest | None = None) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    actor = effective_actor(req.actor if req else "owner")
    return generate_code_wiki(eng, author=actor)


@router.post("/vernal-api/ask")
async def api_ask_codebase(request: Request, req: AskCodebaseRequest) -> dict[str, Any]:
    eng = get_ctx(request).active_slot(request).engine
    try:
        return ask_codebase(eng, req.question, max_matches=max(1, min(req.max_matches, 30)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
