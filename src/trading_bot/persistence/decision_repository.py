"""Decision claims and quota reservations use short transactions, never a network lock."""

from datetime import UTC, datetime
from uuid import uuid4

from trading_bot.persistence.sqlite_repository import SQLiteRepository
from trading_bot.strategy.models import AISettings, APIUsage, DecisionRun


class DecisionRepository:
    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    def get(self, key: str) -> DecisionRun | None:
        with self.repository.transaction() as tx:
            raw = tx.get("decisions", key)
            return DecisionRun.model_validate_json(raw) if raw else None

    def claim(self, record: DecisionRun) -> bool:
        with self.repository.transaction() as tx:
            raw = tx.get("decisions", record.decision_id)
            if raw:
                return False
            tx.put("decisions", record.decision_id, record)
            return True

    def save(self, record: DecisionRun) -> None:
        with self.repository.transaction() as tx:
            tx.put("decisions", record.decision_id, record)

    def registration_error(self, key: str, reason: str) -> None:
        with self.repository.transaction() as tx:
            raw = tx.get("decisions", key)
            if raw:
                record = DecisionRun.model_validate_json(raw)
                if record.status != "REGISTERED":
                    tx.put(
                        "decisions", key, record.model_copy(update={"registration_error": reason})
                    )

    def history(self) -> list[DecisionRun]:
        with self.repository.transaction() as tx:
            return [DecisionRun.model_validate_json(s) for s in tx.records("decisions")]

    def usage(self, decision_id: str | None = None) -> list[APIUsage]:
        with self.repository.transaction() as tx:
            records = [APIUsage.model_validate_json(s) for s in tx.records("api_usage")]
            return [r for r in records if decision_id is None or r.decision_id == decision_id]

    def reserve(self, decision_id: str, settings: AISettings) -> APIUsage:
        now = datetime.now(UTC)
        with self.repository.transaction() as tx:
            rows = [APIUsage.model_validate_json(s) for s in tx.records("api_usage")]
            day = sum(r.at.date() == now.date() for r in rows)
            month = sum((r.at.year, r.at.month) == (now.year, now.month) for r in rows)
            run = sum(r.decision_id == decision_id for r in rows)
            if (
                day >= settings.daily_request_limit
                or month >= settings.monthly_request_limit
                or run >= settings.max_requests_per_run
            ):
                raise ValueError("local API request quota exhausted; no fallback")
            record = APIUsage(
                request_id=str(uuid4()), decision_id=decision_id, at=now, model=settings.model
            )
            tx.put("api_usage", record.request_id, record)
            return record

    def save_usage(self, record: APIUsage) -> None:
        with self.repository.transaction() as tx:
            tx.put("api_usage", record.request_id, record)
