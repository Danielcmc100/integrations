import os
import socket

import pytest
from alembic.config import Config

from alembic import command


def _pg_available() -> bool:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://integrations:integrations@localhost:5432/integrations",
    )
    host = "localhost"
    port = 5432
    if "@" in url:
        hostpart = url.split("@", 1)[1].split("/", 1)[0]
        if ":" in hostpart:
            host_s, port_s = hostpart.split(":", 1)
            host = host_s
            port = int(port_s)
        else:
            host = hostpart
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_available(), reason="postgres not reachable")
def test_alembic_upgrade_and_downgrade() -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
