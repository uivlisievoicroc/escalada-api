import asyncio
import unittest

from escalada.api.live import Cmd, cmd, state_map, state_locks
from escalada.api import live as live_module
from escalada.rate_limit import get_rate_limiter


class SimulationFifteenBoxesTest(unittest.TestCase):
    """Load-style simulation for 15 boxes (command-level, no network/WebSockets)."""

    def setUp(self):
        # Reset global state and relax validation/rate limits for simulation
        state_map.clear()
        state_locks.clear()
        live_module.VALIDATION_ENABLED = False
        rl = get_rate_limiter()
        rl.reset_all()
        rl.max_per_minute = 100000
        rl.max_per_second = 100000
        rl.block_duration = 0

    def test_fifteen_boxes_flow_with_reset_timer_while_running(self):
        async def box_flow(box_id: int):
            # INIT route with 10 holds and 2 competitors
            comp_a = {"nume": f"CompetitorA_{box_id}", "marked": False}
            comp_b = {"nume": f"CompetitorB_{box_id}", "marked": False}
            await cmd(
                Cmd(
                    boxId=box_id,
                    type="INIT_ROUTE",
                    routeIndex=1,
                    holdsCount=10,
                    competitors=[comp_a, comp_b],
                    timerPreset="05:00",
                    categorie=f"Cat_{box_id}",
                )
            )
            sid = state_map[box_id]["sessionId"]

            # START timer, make progress, then reset timer while it is running.
            await cmd(Cmd(boxId=box_id, type="START_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=box_id, type="PROGRESS_UPDATE", delta=1, sessionId=sid))
            await cmd(Cmd(boxId=box_id, type="RESET_PARTIAL", resetTimer=True, sessionId=sid))
            # Capture state immediately after reset to ensure it "stops then resets" even while running.
            st_after_reset = dict(state_map[box_id])

            # Stop is idempotent-ish; this ensures STOP_TIMER doesn't crash after a reset.
            await cmd(Cmd(boxId=box_id, type="STOP_TIMER", sessionId=sid))

            # Register time and submit score for first competitor
            await cmd(
                Cmd(
                    boxId=box_id,
                    type="REGISTER_TIME",
                    registeredTime=12.0,
                    sessionId=sid,
                )
            )
            await cmd(
                Cmd(
                    boxId=box_id,
                    type="SUBMIT_SCORE",
                    competitor=f"CompetitorA_{box_id}",
                    score=1.0,
                    registeredTime=None,
                    sessionId=sid,
                )
            )

            # Request state snapshot (ensure API path works)
            result = await cmd(Cmd(boxId=box_id, type="REQUEST_STATE", sessionId=sid))
            return result, st_after_reset

        async def scenario():
            tasks = [box_flow(bid) for bid in range(1, 16)]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(scenario())

        # Validate results for all 15 boxes
        for i, (res, st_after_reset) in enumerate(results, start=1):
            self.assertEqual(res.get("status"), "ok", f"REQUEST_STATE failed for box {i}")
            st = state_map[i]
            self.assertTrue(st.get("initiated"))

            # Immediately after RESET_PARTIAL(resetTimer=True) the timer must be stopped and reset to preset.
            self.assertEqual(st_after_reset.get("timerState"), "idle")
            self.assertFalse(st_after_reset.get("started"))
            self.assertIsNone(st_after_reset.get("timerEndsAtMs"))
            self.assertEqual(st_after_reset.get("timerRemainingSec"), 300.0)

            # After RESET_PARTIAL(resetTimer=True), timer must not be left running server-side.
            self.assertIn(st.get("timerState"), {"idle", "paused"})
            self.assertFalse(st.get("started"))
            self.assertIsNone(st.get("timerEndsAtMs"))
            self.assertEqual(st.get("timerRemainingSec"), 300.0)

            # After SUBMIT_SCORE, per-climber UI state resets
            self.assertEqual(st.get("timerState"), "idle")
            self.assertFalse(st.get("started"))
            self.assertEqual(st.get("holdCount"), 0.0)

            # Last registered time persisted
            self.assertEqual(st.get("lastRegisteredTime"), 12.0)

            # Current climber advanced to next (second) competitor
            self.assertEqual(st.get("currentClimber"), f"CompetitorB_{i}")

            # Route and holds preserved
            self.assertEqual(st.get("routeIndex"), 1)
            self.assertEqual(st.get("holdsCount"), 10)

        # Cross-box independence checks
        names = {state_map[i]["currentClimber"] for i in range(1, 16)}
        self.assertEqual(len(names), 15, "Each box should track its own competitor independently")


if __name__ == "__main__":
    unittest.main()
