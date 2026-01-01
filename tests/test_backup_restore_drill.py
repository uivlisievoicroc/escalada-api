"""
Backup/restore drill tests (ETAPA 3.1).

Goals:
- snapshots round-trip back into internal live state shape (registeredTime -> lastRegisteredTime)
- full restore can recreate boxes by original ID (UI addressing)
- PK sequence is bumped after explicit inserts (so new boxes can be created)

Skips gracefully if Postgres isn't reachable via TEST_DATABASE_URL/DATABASE_URL.
"""

import json

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from escalada.api.backup import collect_snapshots, restore_snapshots, write_backup_file
from escalada.api import live as live_module
from escalada.db.config import settings
from escalada.db.repositories import BoxRepository, CompetitionRepository

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _make_sessionmaker():
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, pool_pre_ping=True
    )
    SessionMaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, SessionMaker


@pytest_asyncio.fixture
async def db_session():
    engine, SessionMaker = _make_sessionmaker()
    try:
        async with SessionMaker() as session:
            # Availability probe; skip if DB is down
            await session.execute(text("SELECT 1"))
            await session.execute(
                text(
                    "TRUNCATE events, competitors, boxes, competitions RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()
            yield session
            await session.rollback()
    except Exception as exc:  # pragma: no cover
        await engine.dispose()
        pytest.skip(f"Database unavailable ({exc}); start Postgres or set TEST_DATABASE_URL")
    else:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backup_restore_roundtrip_preserves_box_id_and_state(tmp_path, db_session):
    # Ensure deterministic global time criterion state for this test
    async with live_module.time_criterion_lock:
        live_module.time_criterion_enabled = False

    comp_repo = CompetitionRepository(db_session)
    box_repo = BoxRepository(db_session)

    comp = await comp_repo.create("Drill Comp")
    box = await box_repo.create(comp.id, "Box A", routes_count=3, holds_count=25)
    await db_session.commit()

    # Persist a realistic state snapshot (as the live API would do)
    state = {
        "initiated": True,
        "holdsCount": 25,
        "routeIndex": 2,
        "currentClimber": "Ana",
        "started": False,
        "timerState": "idle",
        "holdCount": 3.5,
        "competitors": [{"nume": "Ana", "marked": False}],
        "categorie": "U13F",
        "lastRegisteredTime": 12.34,
        "remaining": 99.0,
        "timerPreset": "05:00",
        "timerPresetSec": 300,
        "scores": {"Ana": [3.5]},
        "times": {"Ana": [12.34]},
        # Also include the global flag in state (backup captures it from state)
        "timeCriterionEnabled": True,
    }

    updated, ok = await box_repo.update_state_with_version(
        box.id,
        current_version=0,
        new_state=state,
        new_session_id=box.session_id,
    )
    assert ok
    await db_session.commit()

    # Collect + write a backup file (exercise disk path)
    snapshots = await collect_snapshots(db_session)
    assert snapshots
    backup_path = await write_backup_file(tmp_path, snapshots)
    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    assert "snapshots" in payload

    snap = next(s for s in payload["snapshots"] if s.get("boxId") == box.id)

    # Simulate a "fresh DB" and in-memory restart
    await db_session.execute(
        text("TRUNCATE events, competitors, boxes, competitions RESTART IDENTITY CASCADE")
    )
    await db_session.commit()
    async with live_module.init_lock:
        live_module.state_map.clear()
        live_module.state_locks.clear()

    restored, conflicts = await restore_snapshots(db_session, [snap])
    await db_session.commit()
    assert conflicts == []
    assert restored == [box.id]

    restored_box = await box_repo.get_by_id(box.id)
    assert restored_box is not None
    assert restored_box.id == box.id
    assert restored_box.box_version == snap.get("boxVersion", 0)
    assert restored_box.session_id == snap.get("sessionId")

    # Critical mapping: snapshot registeredTime -> internal lastRegisteredTime
    assert restored_box.state.get("lastRegisteredTime") == snap.get("registeredTime")
    assert "registeredTime" not in (restored_box.state or {})

    # In-memory state should also be hydrated
    async with live_module.init_lock:
        assert live_module.state_map[box.id]["lastRegisteredTime"] == snap.get(
            "registeredTime"
        )

    # Global time criterion should be restored + broadcasted best-effort
    async with live_module.time_criterion_lock:
        assert live_module.time_criterion_enabled is True

    # Sequence bump: creating a new box should not collide with restored IDs
    restored_comp = await comp_repo.get_by_name("Restored Default")
    assert restored_comp is not None
    new_box = await box_repo.create(restored_comp.id, "Box New", routes_count=1, holds_count=10)
    await db_session.commit()
    assert new_box.id > box.id

