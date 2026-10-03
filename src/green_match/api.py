"""HTTP API 层（FastAPI）。

只做协议转换：解析 JSON、调用 service、把 ServiceError 映射为 HTTP 状态码。
业务规则全部在 service/engine 层，便于脱离 HTTP 直接测试。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .service import ENERGY_KINDS, GreenMatchService, ServiceError

DEFAULT_DB_PATH = os.environ.get("GREEN_MATCH_DB", "green_match.db")


def create_app(db_path: str | None = None, service: GreenMatchService | None = None) -> FastAPI:
    svc = service or GreenMatchService(db_path or DEFAULT_DB_PATH)
    app = FastAPI(title="绿电属性核算服务", version="1.0.0")
    app.state.service = svc

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    async def body(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - 任何 JSON 解析失败都按 422 处理
            raise ServiceError(422, "validation_error", f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ServiceError(422, "validation_error", "请求体必须是 JSON 对象")
        return payload

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/rules")
    async def list_rules():
        """列出可选核算口径（规则版本）。"""
        return {"rules": svc.list_rules()}

    @app.post("/api/ingest/{kind}", status_code=200)
    async def ingest(kind: str, request: Request):
        """导入一个批次。kind: consumption | generation | certificate。

        重复提交相同 idempotency_key + 相同内容返回首次结果（deduplicated=true）；
        相同键不同内容返回 409。
        """
        if kind == "certificates":
            kind = "certificate"
        if kind not in (*ENERGY_KINDS, "certificate"):
            raise ServiceError(422, "invalid_kind",
                               "kind 必须是 consumption | generation | certificates")
        return svc.ingest(kind, await body(request))

    @app.post("/api/reports", status_code=201)
    async def create_report(request: Request):
        """创建报告（选择核算口径），立即产生一次 initial 核算预览。"""
        return svc.create_report(await body(request))

    @app.get("/api/reports/{report_id}")
    async def get_report(report_id: str):
        return svc.get_report(report_id)

    @app.post("/api/reports/{report_id}/seal")
    async def seal_report(report_id: str):
        """封存报告：固化结果并对凭证建立核销锁。幂等。"""
        return svc.seal_report(report_id)

    @app.post("/api/reports/{report_id}/replay")
    async def replay_report(report_id: str):
        """按原定口径重放核算，输出与基准结果的差异。不改变封存结果。"""
        return svc.replay_report(report_id)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        """查看一次核算的完整结果、输入快照与输入摘要。"""
        return svc.get_run(run_id)

    return app


# 注意：不在模块导入时创建默认 app，避免 import 即在当前目录落库文件。
# 启动服务请用 python -m green_match（见 __main__.py）。
