"""
Database integration tests covering versioning, dedup, and constraints.
Requires Postgres reachable via TEST_DATABASE_URL/DATABASE_URL
(defaults to postgresql+asyncpg://escalada:escalada@localhost:5432/escalada_dev,
matching docker-compose db service). Skips gracefully if DB is down.
"""
import pytest
import pytest_asyncio
import uuid
from sqlalchemy import select, and_
from sqlalchemy.exc import IntegrityError

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from escalada.db.config import settings
from escalada.db.models import Competition, Box, Competitor, Event
from escalada.db.repositories import (
    CompetitionRepository,
    BoxRepository,
    CompetitorRepository,
    EventRepository,
)
from sqlalchemy import text

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _make_sessionmaker():
    """Create an isolated sessionmaker and engine for test usage."""
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, pool_pre_ping=True
    )
    SessionMaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, SessionMaker


@pytest_asyncio.fixture
async def db_session():
    """Provide a clean test database session with isolated engine per test."""
    engine, SessionMaker = _make_sessionmaker()
    try:
        async with SessionMaker() as session:
            # Availability probe; skip if DB is down (use TEST_DATABASE_URL or docker-compose up db)
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
async def test_competition_create(db_session):
    """Test creating a competition."""
    repo = CompetitionRepository(db_session)
    comp = await repo.create("Test Comp 1")
    assert comp.name == "Test Comp 1"
    await db_session.commit()


@pytest.mark.asyncio
async def test_unique_competition_name(db_session):
    """Test unique constraint on competition name."""
    repo = CompetitionRepository(db_session)
    await repo.create("Unique Comp")
    db_session.add(Competition(name="Unique Comp"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_box_optimistic_locking(db_session):
    """Test optimistic locking on box version."""
    # Create competition and box
    comp_repo = CompetitionRepository(db_session)
    comp = await comp_repo.create("Lock Test Comp")
    box_repo = BoxRepository(db_session)
    box = await box_repo.create(comp.id, "Boulder A", routes_count=5, holds_count=25)
    await db_session.commit()

    # Update with correct version
    new_state = {"status": "initialized"}
    updated_box, success = await box_repo.update_state_with_version(
        box.id, 0, new_state, "session-1"
    )
    assert success
    assert updated_box.box_version == 1
    assert updated_box.state == new_state
    await db_session.commit()

    # Try to update with stale version
    stale_state = {"status": "stale"}
    updated_box2, success2 = await box_repo.update_state_with_version(
        box.id, 0, stale_state, "session-2"
    )
    assert not success2
    assert updated_box2.box_version == 1
    await db_session.rollback()


@pytest.mark.asyncio
async def test_event_dedup_by_action_id(db_session):
    """Test that events with the same action_id are rejected."""
    comp_repo = CompetitionRepository(db_session)
    comp = await comp_repo.create("Event Test Comp")
    box_repo = BoxRepository(db_session)
    box = await box_repo.create(comp.id, "Boulder B", routes_count=5, holds_count=25)
    await db_session.commit()

    event_repo = EventRepository(db_session)

    # First event with action_id
    action_id = str(uuid.uuid4())
    event1 = await event_repo.log_event(
        comp.id,
        action="INIT_ROUTE",
        payload={"route": 1},
        box_id=box.id,
        action_id=action_id,
        box_version=1,
    )
    await db_session.commit()
    assert event1.action_id == action_id

    # Duplicate action_id should raise
    db_session.add(
        Event(
            id=uuid.uuid4(),
            competition_id=comp.id,
            box_id=box.id,
            action="INIT_ROUTE",
            payload={"route": 1},
            action_id=action_id,
            box_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_competitor_unique_bib(db_session):
    """Test unique constraint on (competition_id, bib)."""
    comp_repo = CompetitionRepository(db_session)
    comp = await comp_repo.create("Bib Test Comp")
    competitor_repo = CompetitorRepository(db_session)

    await competitor_repo.create(comp.id, "Climber A", bib="001", category="Senior")
    await db_session.commit()

    # Same bib in same competition should fail
    db_session.add(
        Competitor(
            competition_id=comp.id,
            name="Climber B",
            bib="001",
            category="Senior",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_seed_idempotent():
    """Test that running seed multiple times doesn't create duplicates."""
    from escalada.scripts.seed import seed

    engine, SessionMaker = _make_sessionmaker()
    try:
        async with SessionMaker() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        await engine.dispose()
        pytest.skip(f"Database unavailable ({exc}); start Postgres or set TEST_DATABASE_URL")

    # First run
    await seed()

    # Get initial counts
    async with SessionMaker() as session:
        comp_result = await session.execute(
            select(Competition).where(Competition.name == "Demo 2026")
        )
        comp = comp_result.scalar_one_or_none()
        assert comp is not None

        boxes = await session.execute(select(Box).where(Box.competition_id == comp.id))
        box_count1 = len(boxes.scalars().all())

        competitors = await session.execute(
            select(Competitor).where(Competitor.competition_id == comp.id)
        )
        comp_count1 = len(competitors.scalars().all())

    # Second run
    await seed()

    # Counts should be the same
    async with SessionMaker() as session:
        boxes = await session.execute(select(Box).where(Box.competition_id == comp.id))
        box_count2 = len(boxes.scalars().all())

        competitors = await session.execute(
            select(Competitor).where(Competitor.competition_id == comp.id)
        )
        comp_count2 = len(competitors.scalars().all())

    assert box_count1 == box_count2
    assert comp_count1 == comp_count2
    await engine.dispose()
