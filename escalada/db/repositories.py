from typing import Any
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from .models import Box, Competition, Competitor, Event, User


class CompetitionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, name: str, starts_at=None, ends_at=None):
        comp = Competition(name=name, starts_at=starts_at, ends_at=ends_at)
        self.session.add(comp)
        await self.session.flush()
        return comp

    async def get_by_id(self, comp_id: int) -> Competition | None:
        return await self.session.get(Competition, comp_id)

    async def get_by_name(self, name: str) -> Competition | None:
        result = await self.session.execute(
            select(Competition).where(Competition.name == name)
        )
        return result.scalar_one_or_none()

    async def list_all(self):
        result = await self.session.execute(select(Competition))
        return result.scalars().all()


class BoxRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        competition_id: int,
        name: str,
        route_index: int = 1,
        routes_count: int = 1,
        holds_count: int = 0,
    ) -> Box:
        box = Box(
            competition_id=competition_id,
            name=name,
            route_index=route_index,
            routes_count=routes_count,
            holds_count=holds_count,
            state={},
            box_version=0,
            session_id=str(uuid.uuid4()),
        )
        self.session.add(box)
        await self.session.flush()
        return box

    async def get_by_id(self, box_id: int) -> Box | None:
        return await self.session.get(Box, box_id)

    async def get_by_competition_and_name(
        self, competition_id: int, name: str
    ) -> Box | None:
        result = await self.session.execute(
            select(Box).where(
                and_(
                    Box.competition_id == competition_id,
                    Box.name == name,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_by_competition(self, competition_id: int):
        result = await self.session.execute(
            select(Box).where(Box.competition_id == competition_id)
        )
        return result.scalars().all()

    async def update_state_with_version(
        self,
        box_id: int,
        current_version: int,
        new_state: dict,
        new_session_id: str | None = None,
    ) -> tuple[Box, bool]:
        """
        Optimistic lock: update state and bump version only if current version matches.
        Returns (box, success). On version mismatch, success=False and box contains current state.
        """
        box = await self.session.get(Box, box_id)
        if not box:
            raise ValueError(f"Box {box_id} not found")

        if box.box_version != current_version:
            return (box, False)

        box.state = new_state
        box.box_version = current_version + 1
        if new_session_id:
            box.session_id = new_session_id
        await self.session.flush()
        return (box, True)

    async def refresh(self, box: Box):
        await self.session.refresh(box)


class CompetitorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        competition_id: int,
        name: str,
        box_id: int | None = None,
        category: str | None = None,
        bib: str | None = None,
        seed: int | None = None,
    ) -> Competitor:
        competitor = Competitor(
            competition_id=competition_id,
            box_id=box_id,
            name=name,
            category=category,
            bib=bib,
            seed=seed,
        )
        self.session.add(competitor)
        await self.session.flush()
        return competitor

    async def get_by_id(self, competitor_id: int) -> Competitor | None:
        return await self.session.get(Competitor, competitor_id)

    async def get_by_bib(self, competition_id: int, bib: str) -> Competitor | None:
        result = await self.session.execute(
            select(Competitor).where(
                and_(
                    Competitor.competition_id == competition_id,
                    Competitor.bib == bib,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_by_competition(self, competition_id: int):
        result = await self.session.execute(
            select(Competitor).where(Competitor.competition_id == competition_id)
        )
        return result.scalars().all()


class EventRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def log_event(
        self,
        competition_id: int,
        action: str,
        payload: dict,
        box_id: int | None = None,
        competitor_id: int | None = None,
        session_id: str | None = None,
        box_version: int = 0,
        action_id: str | None = None,
    ) -> Event:
        """Append an event to the log. Raises IntegrityError on duplicate action_id."""
        event = Event(
            competition_id=competition_id,
            box_id=box_id,
            competitor_id=competitor_id,
            action=action,
            payload=payload,
            session_id=session_id,
            box_version=box_version,
            action_id=action_id,
        )
        self.session.add(event)
        try:
            await self.session.flush()
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_event_dedup" in str(e):
                raise ValueError(f"Duplicate action_id: {action_id}") from e
            raise
        return event

    async def get_events_for_box(
        self, box_id: int, from_version: int = 0, to_version: int | None = None
    ):
        """Get events for a box within a version range."""
        query = select(Event).where(
            and_(
                Event.box_id == box_id,
                Event.box_version >= from_version,
            )
        )
        if to_version is not None:
            query = query.where(Event.box_version <= to_version)
        query = query.order_by(Event.created_at.asc())
        result = await self.session.execute(query)
        return result.scalars().all()


class UserRepository:
    @staticmethod
    async def get_by_username(session: AsyncSession, username: str) -> User | None:
        result = await session.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    @staticmethod
    async def create_user(
        session: AsyncSession,
        username: str,
        password_hash: str,
        role: str = "viewer",
        assigned_boxes: list[int] | None = None,
        is_active: bool = True,
    ) -> User:
        user = User(
            username=username,
            password_hash=password_hash,
            role=role,
            assigned_boxes=assigned_boxes or [],
            is_active=is_active,
        )
        session.add(user)
        await session.flush()
        return user

    @staticmethod
    async def upsert_judge(
        session: AsyncSession,
        box_id: int,
        password_hash: str,
        username: str | None = None,
    ) -> User:
        """Create or update judge user for a box."""
        username = username or f"Box {box_id}"
        user = await UserRepository.get_by_username(session, username)
        if not user:
            user = User(
                username=username,
                password_hash=password_hash,
                role="judge",
                assigned_boxes=[box_id],
                is_active=True,
            )
            session.add(user)
        else:
            user.password_hash = password_hash
            user.role = "judge"
            user.assigned_boxes = [box_id]
            user.is_active = True
        await session.flush()
        return user
