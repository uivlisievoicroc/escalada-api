"""
Idempotent seed script for Escalada database.
Run via: python -m escalada.scripts.seed
"""
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, and_

from escalada.db.database import AsyncSessionLocal
from escalada.db.models import Competition, Box, Competitor
from escalada.db.repositories import (
    CompetitionRepository,
    BoxRepository,
    CompetitorRepository,
)


async def seed():
    async with AsyncSessionLocal() as session:
        # Competition
        comp_repo = CompetitionRepository(session)
        comp = await comp_repo.get_by_name("Demo 2026")
        if not comp:
            comp = await comp_repo.create(
                name="Demo 2026",
                starts_at=datetime.now(timezone.utc),
                ends_at=datetime.now(timezone.utc),
            )
            print(f"✓ Created competition: {comp.name}")
        else:
            print(f"✓ Competition {comp.name} already exists")

        box_repo = BoxRepository(session)
        competitor_repo = CompetitorRepository(session)

        # Boxes
        for box_idx in range(1, 4):
            box_name = f"Boulder {box_idx}"
            box = await box_repo.get_by_competition_and_name(comp.id, box_name)
            if not box:
                box = await box_repo.create(
                    competition_id=comp.id,
                    name=box_name,
                    route_index=1,
                    routes_count=5,
                    holds_count=25,
                )
                print(f"✓ Created box: {box.name}")
            else:
                print(f"✓ Box {box_name} already exists")

            # Competitors in each box
            for comp_idx in range(1, 6):
                comp_name = f"Competitor {box_idx}-{comp_idx}"
                result = await session.execute(
                    select(Competitor).where(
                        and_(
                            Competitor.competition_id == comp.id,
                            Competitor.name == comp_name,
                        )
                    )
                )
                if not result.scalar_one_or_none():
                    await competitor_repo.create(
                        competition_id=comp.id,
                        box_id=box.id,
                        name=comp_name,
                        category="Seniori",
                        bib=f"{box_idx:02d}{comp_idx:02d}",
                        seed=comp_idx,
                    )
                    print(f"  ✓ Created competitor: {comp_name}")

        await session.commit()
        print("\n✓ Seed completed successfully")


if __name__ == "__main__":
    asyncio.run(seed())
