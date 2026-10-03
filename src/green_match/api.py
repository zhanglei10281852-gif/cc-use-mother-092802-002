"""HTTP API 层：把领域服务暴露为 REST 接口。

写操作支持 `Idempotency-Key` 请求头；命中重放时响应头带
`X-Idempotent-Replay: true` 并返回首个响应。
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal, Optional

from fastapi import FastAPI, Header, Response
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field

from .errors import DomainError
from .rules import CALIBERS, DEFAULT_RULE_VERSION, SUPPORTED_RULE_VERSIONS
from .service import GreenMatchService


class MeterIntervalIn(BaseModel):
    meter_id: str = Field(min_length=1)
    kind: Literal["consumption", "generation"]
    region: str = Field(min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    kwh: Decimal = Field(ge=Decimal("0"))
    revision: int = Field(default=1, ge=1)
    source: Optional[str] = None


class MeterIntervalBatch(BaseModel):
    intervals: list[MeterIntervalIn] = Field(min_length=1)


class AttributeLotIn(BaseModel):
    lot_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    available_kwh: Decimal = Field(gt=Decimal("0"))
    source_digest: str = Field(min_length=1)


class AttributeLotBatch(BaseModel):
    lots: list[AttributeLotIn] = Field(min_length=1)


class ReportCreate(BaseModel):
    enterprise_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    caliber: str = Field(min_length=1)


class SealRequest(BaseModel):
    run_id: Optional[str] = None


def create_app(db_path: Optional[str] = None) -> FastAPI:
    service = GreenMatchService(db_path or os.environ.get("GREEN_MATCH_DB", "green_match.db"))
    app = FastAPI(title="绿电属性核算服务", version="1.0.0")

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/calibers")
    def calibers():
        return {
            "default_rule_version": DEFAULT_RULE_VERSION,
            "supported_rule_versions": list(SUPPORTED_RULE_VERSIONS),
            "calibers": [
                {
                    "name": c.name,
                    "granularity": c.granularity,
                    "region_match": c.region_match,
                    "cert_window": c.cert_window,
                    "description": c.description,
                }
                for c in CALIBERS.values()
            ],
        }

    @app.post("/imports/meter-intervals")
    def import_meter_intervals(
        body: MeterIntervalBatch,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result, replayed = service.import_meter_intervals(
            [item.model_dump() for item in body.intervals], idempotency_key
        )
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return result

    @app.post("/imports/attribute-lots")
    def import_attribute_lots(
        body: AttributeLotBatch,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result, replayed = service.import_attribute_lots(
            [item.model_dump() for item in body.lots], idempotency_key
        )
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
        return result

    @app.post("/reports", status_code=201)
    def create_report(
        body: ReportCreate,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result, replayed = service.create_report(
            body.enterprise_id, body.region, body.period_start, body.period_end,
            body.caliber, idempotency_key,
        )
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
            response.status_code = 200
        return result

    @app.get("/reports/{report_id}")
    def get_report(report_id: str):
        return service.get_report(report_id)

    @app.post("/reports/{report_id}/runs", status_code=201)
    def run_accounting(
        report_id: str,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result, replayed = service.run_accounting(report_id, idempotency_key)
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
            response.status_code = 200
        return result

    @app.post("/reports/{report_id}/seal")
    def seal_report(report_id: str, body: Optional[SealRequest] = None):
        return service.seal_report(report_id, body.run_id if body else None)

    @app.post("/reports/{report_id}/void")
    def void_report(report_id: str):
        return service.void_report(report_id)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return service.get_run(run_id)

    @app.post("/runs/{run_id}/replay", status_code=201)
    def replay_run(
        run_id: str,
        response: Response,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result, replayed = service.replay_run(run_id, idempotency_key)
        if replayed:
            response.headers["X-Idempotent-Replay"] = "true"
            response.status_code = 200
        return result

    @app.get("/lots/{lot_id}/usage")
    def lot_usage(lot_id: str):
        return service.get_lot_usage(lot_id)

    return app
