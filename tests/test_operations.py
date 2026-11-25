import pytest

from ifood.operations import confirm, prepare, validate_schedule

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


class FakeClient:
    def __init__(self):
        self.config = type("Config", (), {"merchant_id": "550e8400-e29b-41d4-a716-446655440000"})()
        self.shifts = [
            {"dayOfWeek": day, "start": "09:00:00", "duration": 360} for day in DAYS
        ]
        self.writes = 0

    def opening_hours(self):
        return [{"shifts": self.shifts}]

    def put_opening_hours(self, shifts):
        self.writes += 1
        self.shifts = shifts


def test_legacy_proposal_cannot_bypass_persistent_audit():
    client = FakeClient()
    proposed = [
        {"dayOfWeek": day, "start": "10:00:00", "duration": 360} for day in DAYS
    ]
    proposal = prepare(client, "operator", proposed)
    assert client.writes == 0
    with pytest.raises(PermissionError, match="SyncEngine"):
        confirm(client, "operator", proposal["proposal_id"])
    assert client.writes == 0


def test_incomplete_week_and_overlapping_shifts_rejected():
    with pytest.raises(ValueError, match="sete dias"):
        validate_schedule([{"dayOfWeek": "MONDAY", "start": "09:00:00", "duration": 360}])
    shifts = [{"dayOfWeek": day, "start": "09:00:00", "duration": 360} for day in DAYS]
    shifts.append({"dayOfWeek": "MONDAY", "start": "10:00:00", "duration": 120})
    with pytest.raises(ValueError, match="sobrepostos"):
        validate_schedule(shifts)
