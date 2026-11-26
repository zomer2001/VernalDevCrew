"""Read-only execution capability checks using the same identity as background dispatch."""
from __future__ import annotations

import asyncio
import copy

from autodev import public_pool
from autodev.loopforge.client import LoopForgeClient


async def inspect(engine) -> dict:
    settings = engine.settings
    checks = []

    def add(key, ok, message):
        checks.append({"key": key, "ok": ok, "message": message})

    add("binding", bool(settings.loopforge_project_id), "项目已绑定" if settings.loopforge_project_id else "请先绑定原生项目")
    add("backend", settings.runtime_backend == "loopforge", "使用现有规划执行链路")
    token = settings.service_token()
    add("identity", bool(token), "后台执行身份已配置" if token else "后台执行身份未配置，网页登录态不能代替后台执行身份")
    runners, user = [], {}
    if token:
        try:
            client = LoopForgeClient(settings.loopforge_url, token)
            runners, user = await asyncio.wait_for(asyncio.gather(client.list_runners(), client.auth_current()), timeout=8)
            add("control_plane", True, "后台身份可以连接执行服务")
        except Exception:
            add("control_plane", False, "后台身份无法连接执行服务或权限不足，请检查服务和执行身份")
    public = public_pool.status()
    runner, runtimes = public_pool.select_runner(settings, runners, user)
    selected = settings.execution_pool
    if selected == "auto":
        selected = "local_runner" if runner else "public_docker"
    probe = copy.copy(settings)
    probe.selected_pool = selected
    if selected == "local_runner":
        add("runner", bool(runner), "本机执行机与所需执行器已就绪" if runner else "没有一台在线本机执行机具备当前角色所需的执行器和凭据")
        if runtimes:
            probe.runner_harness = next((name for name, info in runtimes.items() if info.get("ready")), "")
    else:
        for key, title in (("image_ready", "沙箱镜像"), ("key_configured", "公用模型凭据"), ("identity_configured", "公用执行身份")):
            add(key, bool(public[key]), title + ("已配置" if public[key] else "未就绪"))
        probe.runner_harness = next((name for name, info in public["runtimes"].items() if info.get("ready")), "")
    roles = []
    for role in ("pm", "director", "coder", "qa_lead"):
        harness = probe.harness_for(role)
        model = probe.model_for(role)
        if selected == "public_docker":
            add(f"{role}.runtime", bool(public["runtimes"].get(harness, {}).get("ready")), f"{role} 使用 {harness} 的公用沙箱能力")
        if harness == "dsh":
            add(f"{role}.model", bool(model), f"{role} 的模型" + ("已配置" if model else "未明确配置"))
        roles.append({"role": role, "harness": harness, "model": model or "使用执行器登录配置"})
    return {"ready": all(x["ok"] for x in checks), "selected_pool": selected, "checks": checks, "roles": roles,
            "verification": "只检查派发前配置；模型访问、项目授权和实际执行结果仍以原生派发收据为准。"}
