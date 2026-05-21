from sqlalchemy import DateTime, Integer, String, Table, UniqueConstraint
from sqlalchemy.types import TypeEngine

from integration.models import (
    Base,
    CardIssueLink,
    SyncSource,
    WebhookEventLog,
    WebhookSource,
    WebhookStatus,
)


def _table(model: type) -> Table:
    table = Base.metadata.tables[model.__tablename__]  # type: ignore[attr-defined]
    return table


def test_card_issue_link_table() -> None:
    table = _table(CardIssueLink)
    assert table.name == "card_issue_link"
    pk_cols = [c.name for c in table.primary_key.columns]
    assert pk_cols == ["plane_card_id"]
    assert isinstance(table.c["plane_card_id"].type, String)
    assert isinstance(table.c["gh_issue_number"].type, Integer)
    last_synced: TypeEngine[object] = table.c["last_synced_at"].type
    assert isinstance(last_synced, DateTime)
    assert last_synced.timezone is True
    assert table.c["last_synced_at"].nullable is False


def test_card_issue_link_unique_repo_issue() -> None:
    table = _table(CardIssueLink)
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert any(
        sorted(col.name for col in c.columns) == ["gh_issue_number", "gh_repo"]
        for c in uniques
    )


def test_webhook_event_log_table() -> None:
    table = _table(WebhookEventLog)
    assert table.name == "webhook_event_log"
    pk_cols = [c.name for c in table.primary_key.columns]
    assert pk_cols == ["id"]
    assert table.c["processed_at"].nullable is True
    assert table.c["status"].nullable is False


def test_enum_members() -> None:
    assert {e.value for e in SyncSource} == {"plane", "github"}
    assert {e.value for e in WebhookSource} == {"plane", "github"}
    assert {e.value for e in WebhookStatus} == {"pending", "processed", "failed"}
