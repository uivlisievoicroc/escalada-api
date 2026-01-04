import asyncio
import unittest
from unittest.mock import AsyncMock, patch


class _BoxStub:
    def __init__(self):
        self.id = 1
        self.competition_id = 99
        self.routes_count = 2
        self.state = {}
        self.box_version = 0
        self.session_id = "sid-1"


class FetchBoxSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        from escalada.api import live

        live.state_map.clear()

    def test_fetch_box_snapshot_prefers_live_scores_when_db_state_missing(self):
        from escalada.api import live
        from escalada.api.backup import _fetch_box_snapshot

        async def scenario():
            live.state_map[1] = {
                "categorie": "U13F",
                "scores": {"Ana": [3.5, 4.0]},
                "times": {"Ana": [12.34, 11.0]},
                "timeCriterionEnabled": True,
            }
            with patch(
                "escalada.api.backup.repos.BoxRepository.get_by_id",
                new=AsyncMock(return_value=_BoxStub()),
            ), patch(
                "escalada.api.backup.repos.CompetitorRepository.list_by_competition",
                new=AsyncMock(return_value=[]),
            ):
                return await _fetch_box_snapshot(None, 1)

        snap = asyncio.run(scenario())
        self.assertEqual(snap["boxId"], 1)
        self.assertEqual(snap["categorie"], "U13F")
        self.assertEqual(snap["scores"], {"Ana": [3.5, 4.0]})
        self.assertEqual(snap["times"], {"Ana": [12.34, 11.0]})
        self.assertTrue(snap["timeCriterionEnabled"])


if __name__ == "__main__":
    unittest.main()

