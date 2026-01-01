import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String,
    Integer,
    ForeignKey,
    UniqueConstraint,
    DateTime,
    Boolean,
    Float,
    JSON,
    Index,
    Text,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(20), default="viewer"
    )  # admin | judge | viewer
    assigned_boxes: Mapped[list[int] | None] = mapped_column(
        JSONB, nullable=True, default=list
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            "role in ('admin','judge','viewer')", name="ck_users_role_valid"
        ),
    )


class Competition(Base):
    __tablename__ = "competitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    boxes: Mapped[list["Box"]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    competitors: Mapped[list["Competitor"]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="competition", cascade="all, delete-orphan"
    )


class Box(Base):
    __tablename__ = "boxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    route_index: Mapped[int] = mapped_column(Integer, default=1)
    routes_count: Mapped[int] = mapped_column(Integer, default=1)
    holds_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    box_version: Mapped[int] = mapped_column(Integer, default=0)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    competition: Mapped["Competition"] = relationship(back_populates="boxes")
    competitors: Mapped[list["Competitor"]] = relationship(
        back_populates="box", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="box", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "competition_id", "name", name="uq_box_name_per_competition"
        ),
    )


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), index=True
    )
    box_id: Mapped[int | None] = mapped_column(
        ForeignKey("boxes.id", ondelete="CASCADE"), index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bib: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    competition: Mapped["Competition"] = relationship(back_populates="competitors")
    box: Mapped["Box | None"] = relationship(back_populates="competitors")

    __table_args__ = (
        UniqueConstraint(
            "competition_id", "bib", name="uq_competitor_bib_per_competition"
        ),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), index=True
    )
    box_id: Mapped[int | None] = mapped_column(
        ForeignKey("boxes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    competitor_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    box_version: Mapped[int] = mapped_column(Integer, default=0, index=True)
    action_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    competition: Mapped["Competition"] = relationship(back_populates="events")
    box: Mapped["Box | None"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint(
            "box_id", "action_id", name="uq_event_dedup"
        ),
        Index(
            "idx_events_competition_box_version",
            "competition_id",
            "box_id",
            "box_version",
        ),
    )
