"""Propostas de horário vinculadas ao estado observado e ao operador."""
import datetime
import hashlib
import json
import threading
import time
import uuid
from itertools import pairwise
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OpeningHoursClient(Protocol):
    config: Any

    def opening_hours(self) -> list[dict[str, Any]]: ...

    def put_opening_hours(self, shifts: list[dict[str, Any]]) -> Any: ...


class Shift(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dayOfWeek: str
    start: str
    duration: int = Field(gt=0, le=1440)

    @field_validator("dayOfWeek")
    @classmethod
    def valid_day(cls, value: str) -> str:
        if value not in {"MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}:
            raise ValueError("Dia da semana inválido")
        return value

    @field_validator("start")
    @classmethod
    def valid_start(cls, value: str) -> str:
        try:
            datetime.time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Horário inválido") from exc
        if len(value) != 8:
            raise ValueError("Use HH:MM:SS")
        return value


def normalize(shifts: list[dict]) -> list[dict]:
    return sorted(({key: item[key] for key in ("dayOfWeek", "start", "duration")} for item in shifts),
                  key=lambda item: (item["dayOfWeek"], item["start"], item["duration"]))


def validate_schedule(shifts: list[dict]) -> list[dict]:
    if not shifts or len(shifts) > 56:
        raise ValueError("Informe de 1 a 56 turnos para a grade completa")
    clean = [Shift.model_validate(shift).model_dump() for shift in shifts]
    required_days = {"MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}
    missing_days = required_days - {shift["dayOfWeek"] for shift in clean}
    if missing_days:
        raise ValueError(
            "A grade substitui todos os horários; informe explicitamente os sete dias da semana."
        )
    for day in {shift["dayOfWeek"] for shift in clean}:
        intervals = sorted((int(s["start"][:2]) * 60 + int(s["start"][3:5]), s["duration"]) for s in clean if s["dayOfWeek"] == day)
        if any(start + duration > 1440 for start, duration in intervals):
            raise ValueError("Turno ultrapassa o dia")
        if any(a[0] + a[1] > b[0] for a, b in pairwise(intervals)):
            raise ValueError("Turnos sobrepostos")
    return normalize(clean)


def digest(value: list[dict]) -> str:
    return hashlib.sha256(json.dumps(normalize(value), sort_keys=True).encode()).hexdigest()


_proposals: dict[str, dict] = {}
_lock = threading.Lock()


def prepare(client: OpeningHoursClient, actor: str, shifts: list[dict]) -> dict:
    proposed = validate_schedule(shifts)
    current = client.opening_hours()
    current_shifts = [shift for group in current for shift in group.get("shifts", [])]
    proposal_id = str(uuid.uuid4())
    with _lock:
        _proposals[proposal_id] = {"actor": actor, "merchant_id": client.config.merchant_id,
                                   "current_hash": digest(current_shifts), "shifts": proposed,
                                   "expires": time.monotonic() + 300}
    return {"proposal_id": proposal_id, "merchant_id": client.config.merchant_id,
            "current": normalize(current_shifts), "proposed": proposed, "replaces_entire_schedule": True}


def confirm(client: OpeningHoursClient, actor: str, proposal_id: str) -> dict:
    # The original pilot proposal lived only in process memory and had no
    # durable execution/audit transaction. Keep the endpoint read-only until
    # callers migrate to SyncEngine proposals.
    raise PermissionError(
        "Confirmação do fluxo legado desativada; use uma proposta auditada do SyncEngine."
    )
