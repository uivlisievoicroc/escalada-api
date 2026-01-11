import asyncio
import unittest


class FetchBoxSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        from escalada.api import live

        live.state_map.clear()

    def test_fetch_box_snapshot_uses_live_state(self):
        from escalada.api import live
        from escalada.api.backup import _fetch_box_snapshot

        async def scenario():
            live.state_map[1] = {
                "categorie": "U13F",
                "scores": {"Ana": [3.5, 4.0]},
                "times": {"Ana": [12.34, 11.0]},
                "timeCriterionEnabled": True,
            }
            return await _fetch_box_snapshot(1)

        snap = asyncio.run(scenario())
        self.assertEqual(snap["boxId"], 1)
        self.assertEqual(snap["categorie"], "U13F")
        self.assertEqual(snap["scores"], {"Ana": [3.5, 4.0]})
        self.assertEqual(snap["times"], {"Ana": [12.34, 11.0]})
        self.assertTrue(snap["timeCriterionEnabled"])


if __name__ == "__main__":
    unittest.main()
