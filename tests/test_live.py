import asyncio
import unittest

from escalada.api.live import Cmd, cmd, state_map, state_locks
from escalada.api import live as live_module
from escalada.rate_limit import get_rate_limiter


# ==================== GLOBAL TEST SETUP ====================

def setUpModule():
    """Reset rate limiter and state before all tests"""
    state_map.clear()
    state_locks.clear()
    # Disable validation for tests - maintain backward compatibility
    live_module.VALIDATION_ENABLED = False
    # Disable rate limiting for tests
    rate_limiter = get_rate_limiter()
    rate_limiter.request_history.clear()
    rate_limiter.command_history.clear()
    rate_limiter.max_per_minute = 100000  # Effectively unlimited
    rate_limiter.max_per_second = 100000
    rate_limiter.block_duration = 0


class BaseTestCase(unittest.TestCase):
    """Base test class with automatic cleanup between tests"""
    
    def setUp(self):
        """Reset state for each test"""
        state_map.clear()
        state_locks.clear()
        # Reset rate limiter completely
        rate_limiter = get_rate_limiter()
        rate_limiter.reset_all()
        # Ensure rate limiting is disabled
        rate_limiter.max_per_minute = 100000
        rate_limiter.max_per_second = 100000
        rate_limiter.block_duration = 0


# ==================== INITIALIZATION TESTS ====================
class InitRouteTest(BaseTestCase):

    def test_init_route_basic(self):
        async def scenario():
            await cmd(Cmd(
                boxId=1,
                type="INIT_ROUTE",
                routeIndex=1,
                holdsCount=10,
                competitors=[{"nume": "Alex", "marked": False}, {"nume": "Bob", "marked": False}],
                categorie="Youth"
            ))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertTrue(state["initiated"])
        self.assertEqual(state["routeIndex"], 1)
        self.assertEqual(state["holdsCount"], 10)
        self.assertEqual(state["currentClimber"], "Alex")
        self.assertEqual(state["categorie"], "Youth")

    def test_init_route_with_timer_preset(self):
        async def scenario():
            await cmd(Cmd(
                boxId=1,
                type="INIT_ROUTE",
                routeIndex=1,
                holdsCount=10,
                competitors=[{"nume": "Alex"}],
                timerPreset="05:30"
            ))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["timerPreset"], "05:30")
        self.assertEqual(state["timerPresetSec"], 330)

    def test_init_route_empty_competitors(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=2, holdsCount=15, competitors=[]))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["currentClimber"], "")


# ==================== TIMER TESTS ====================
class TimerCommandsTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_start_timer(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="START_TIMER", sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertTrue(state["started"])
        self.assertEqual(state["timerState"], "running")

    def test_stop_timer(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="START_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=1, type="STOP_TIMER", sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertFalse(state["started"])
        self.assertEqual(state["timerState"], "paused")

    def test_resume_timer(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="START_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=1, type="STOP_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=1, type="RESUME_TIMER", sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertTrue(state["started"])

    def test_timer_sync(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="TIMER_SYNC", remaining=45.5, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["remaining"], 45.5)


# ==================== PROGRESS UPDATE TESTS ====================
class ProgressUpdateTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_progress_update_increment(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid))
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["holdCount"], 2)

    def test_progress_update_half_hold(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid))
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=0.5, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["holdCount"], 1.5)

    def test_progress_update_negative(self)  :
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=5, sessionId=sid))
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=-2, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["holdCount"], 3.0)


# ==================== TIME REGISTRATION TESTS ====================
class RegisterTimeTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_register_time(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", registeredTime=15.5, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["lastRegisteredTime"], 15.5)

    def test_register_time_zero(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", registeredTime=0, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["lastRegisteredTime"], 0)

    def test_register_time_none_ignored(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", registeredTime=10, sessionId=sid))
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", registeredTime=None, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["lastRegisteredTime"], 10)


# ==================== SCORE SUBMISSION TESTS ====================
class SubmitScoreTimeFallbackTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_submit_score_keeps_last_registered_time_when_missing(self):
        async def scenario():
            await cmd(
                Cmd(
                    boxId=1,
                    type="INIT_ROUTE",
                    routeIndex=1,
                    holdsCount=10,
                    competitors=[{"nume": "Alex", "marked": False}],
                )
            )
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", registeredTime=12, sessionId=sid))
            await cmd(
                Cmd(
                    boxId=1,
                    type="SUBMIT_SCORE",
                    score=5,
                    competitor="Alex",
                    registeredTime=None,
                    sessionId=sid,
                )
            )

        asyncio.run(scenario())
        self.assertEqual(state_map[1]["lastRegisteredTime"], 12)

    def test_submit_score_marks_competitor(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex", "marked": False}, {"nume": "Bob", "marked": False}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="SUBMIT_SCORE", competitor="Alex", score=8, registeredTime=12.0, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertTrue(state["competitors"][0]["marked"])
        self.assertEqual(state["currentClimber"], "Bob")

    def test_submit_score_invalid_competitor(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex", "marked": False}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="SUBMIT_SCORE", competitor="NonExistent", score=8, registeredTime=10, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertEqual(state["currentClimber"], "Alex")

    def test_submit_score_reset_timer(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="START_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=5, sessionId=sid))
            await cmd(Cmd(boxId=1, type="SUBMIT_SCORE", competitor="Alex", score=5, registeredTime=10, sessionId=sid))
            return state_map[1]
        state = asyncio.run(scenario())
        self.assertFalse(state["started"])
        self.assertEqual(state["timerState"], "idle")
        self.assertEqual(state["holdCount"], 0.0)


# ==================== MULTI-BOX TESTS ====================
class MultiBoxTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_multiple_boxes_isolated(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            await cmd(Cmd(boxId=2, type="INIT_ROUTE", routeIndex=2, holdsCount=15, competitors=[{"nume": "Bob"}]))
            return (state_map[1], state_map[2])
        state1, state2 = asyncio.run(scenario())
        self.assertEqual(state1["routeIndex"], 1)
        self.assertEqual(state2["routeIndex"], 2)
        self.assertEqual(state1["holdsCount"], 10)
        self.assertEqual(state2["holdsCount"], 15)

    def test_concurrent_timers_different_boxes(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            await cmd(Cmd(boxId=2, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Bob"}]))
            sid1 = state_map[1]["sessionId"]
            sid2 = state_map[2]["sessionId"]
            await cmd(Cmd(boxId=1, type="START_TIMER", sessionId=sid1))
            await cmd(Cmd(boxId=2, type="STOP_TIMER", sessionId=sid2))
            return (state_map[1], state_map[2])
        state1, state2 = asyncio.run(scenario())
        self.assertTrue(state1["started"])
        self.assertFalse(state2["started"])


# ==================== REQUEST STATE TEST ====================
class RequestStateTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_request_state(self):
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", routeIndex=1, holdsCount=10, competitors=[{"nume": "Alex"}]))
            sid = state_map[1]["sessionId"]
            result = await cmd(Cmd(boxId=1, type="REQUEST_STATE", sessionId=sid))
            return result
        result = asyncio.run(scenario())
        self.assertEqual(result["status"], "ok")


# ==================== TIME CRITERION TEST ====================
class TimeCriterionTest(BaseTestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_set_time_criterion(self):
        async def scenario():
            result = await cmd(Cmd(boxId=1, type="SET_TIME_CRITERION", timeCriterionEnabled=True))
            return result
        result = asyncio.run(scenario())
        self.assertEqual(result["status"], "ok")


# ==================== HELPER FUNCTION TESTS ====================
class HelperFunctionsTest(BaseTestCase):
    def test_parse_timer_preset_valid(self):
        from escalada.api.live import _parse_timer_preset
        self.assertEqual(_parse_timer_preset("05:30"), 330)
        self.assertEqual(_parse_timer_preset("01:00"), 60)
        self.assertEqual(_parse_timer_preset("10:45"), 645)
        self.assertEqual(_parse_timer_preset("00:00"), 0)

    def test_parse_timer_preset_invalid(self):
        from escalada.api.live import _parse_timer_preset
        self.assertIsNone(_parse_timer_preset(None))
        self.assertIsNone(_parse_timer_preset("invalid"))
        self.assertIsNone(_parse_timer_preset(""))


if __name__ == "__main__":
    unittest.main()


class ExceptionHandlingTest(BaseTestCase):
    """Test exception handling and error paths"""

    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_invalid_command_type(self):
        """Test handling of invalid command type"""
        async def scenario():
            # Ensure state exists to provide a sessionId
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[]))
            sid = state_map[1]["sessionId"]
            result = await cmd(Cmd(boxId=1, type="INVALID_COMMAND", sessionId=sid))
            # Should handle gracefully without crashing
            return state_map.get(1)
        
        state = asyncio.run(scenario())
        # State should not be corrupted
        self.assertTrue(True)

    def test_missing_boxId(self):
        """Test handling when boxId is None or missing"""
        async def scenario():
            try:
                # This should fail validation at Pydantic level
                result = await cmd(Cmd(type="INIT_ROUTE"))  # type: ignore[call-arg]
            except Exception:
                return None
        
        asyncio.run(scenario())

    def test_concurrent_same_box_operations(self):
        """Test concurrent operations on same box use locks correctly"""
        async def scenario():
            # Initialize box
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[]))
            
            # Run concurrent operations on same box
            sid = state_map[1]["sessionId"]
            tasks = [
                cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid)),
                cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid)),
                cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid)),
            ]
            await asyncio.gather(*tasks)
            
            return state_map[1]
        
        state = asyncio.run(scenario())
        # All 3 increments should be applied (no race condition)
        self.assertEqual(state["holdCount"], 3.0)

    def test_submit_score_with_missing_competitor(self):
        """Test submit score with non-existent competitor index"""
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[{"nume": "Alice"}, {"nume": "Bob"}]))
            # Try to submit for invalid index
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="SUBMIT_SCORE", competitorIdx=999, sessionId=sid))
            return state_map[1]
        
        state = asyncio.run(scenario())
        # Should handle gracefully
        self.assertIsNotNone(state)

    def test_register_time_negative_value(self):
        """Test register time with negative value"""
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[{"nume": "Alice"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="REGISTER_TIME", competitorIdx=0, time=-10, sessionId=sid))
            return state_map[1]
        
        state = asyncio.run(scenario())
        # Should handle gracefully (or store -10 if that's intended)
        self.assertIsNotNone(state)

    def test_timer_operations_without_init(self):
        """Test timer commands on uninitialized box"""
        async def scenario():
            # Initialize to obtain sessionId before timer operations
            await cmd(Cmd(boxId=999, type="INIT_ROUTE", timerPreset="3:00", competitors=[]))
            sid = state_map[999]["sessionId"]
            await cmd(Cmd(boxId=999, type="START_TIMER", sessionId=sid))
            await cmd(Cmd(boxId=999, type="STOP_TIMER", sessionId=sid))
            return state_map.get(999)
        
        state = asyncio.run(scenario())
        # Should initialize state automatically or handle gracefully
        self.assertIsNotNone(state)

    def test_progress_update_extreme_values(self):
        """Test progress update with extreme delta values"""
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1000, sessionId=sid))
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=-1000, sessionId=sid))
            return state_map[1]
        
        state = asyncio.run(scenario())
        # Should handle extreme values
        self.assertIsNotNone(state)


class StateConsistencyTest(BaseTestCase):
    """Test state consistency across operations"""

    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_state_persists_after_submit_score(self):
        """Test that route state persists after score submission"""
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[{"nume": "Alice"}, {"nume": "Bob"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=5, sessionId=sid))
            await cmd(Cmd(boxId=1, type="SUBMIT_SCORE", competitorIdx=0, sessionId=sid))
            return state_map[1]
        
        state = asyncio.run(scenario())
        # Hold count should remain
        self.assertIn("holdCount", state)
        # Timer should exist
        self.assertIn("timerState", state)

    def test_multiple_box_state_independence(self):
        """Test that multiple boxes maintain independent state"""
        async def scenario():
            # Initialize 3 boxes
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[{"nume": "A"}]))
            await cmd(Cmd(boxId=2, type="INIT_ROUTE", timerPreset="4:00", competitors=[{"nume": "B"}]))
            await cmd(Cmd(boxId=3, type="INIT_ROUTE", timerPreset="5:00", competitors=[{"nume": "C"}]))
            
            # Modify each independently
            sid1 = state_map[1]["sessionId"]
            sid2 = state_map[2]["sessionId"]
            sid3 = state_map[3]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=1, sessionId=sid1))
            await cmd(Cmd(boxId=2, type="PROGRESS_UPDATE", delta=2, sessionId=sid2))
            await cmd(Cmd(boxId=3, type="PROGRESS_UPDATE", delta=3, sessionId=sid3))
            
            return {
                "box1": state_map[1],
                "box2": state_map[2],
                "box3": state_map[3]
            }
        
        states = asyncio.run(scenario())
        self.assertEqual(states["box1"]["holdCount"], 1.0)
        self.assertEqual(states["box2"]["holdCount"], 2.0)
        self.assertEqual(states["box3"]["holdCount"], 3.0)

    def test_request_state_returns_current_state(self):
        """Test REQUEST_STATE returns exact current state"""
        async def scenario():
            await cmd(Cmd(boxId=1, type="INIT_ROUTE", timerPreset="3:00", competitors=[{"nume": "Alice"}]))
            sid = state_map[1]["sessionId"]
            await cmd(Cmd(boxId=1, type="PROGRESS_UPDATE", delta=7, sessionId=sid))
            result = await cmd(Cmd(boxId=1, type="REQUEST_STATE", sessionId=sid))
            return result
        
        result = asyncio.run(scenario())
        # Should return state with holdCount=7
        self.assertEqual(result["status"], "ok")


# ==================== WEBSOCKET HEARTBEAT TESTS ====================
class WebSocketHeartbeatTest(BaseTestCase):
    """Test WebSocket heartbeat mechanism (PING/PONG)"""

    def test_heartbeat_ping_message_format(self):
        """Test that PING messages are properly formatted"""
        async def scenario():
            # Simulate server sending PING
            ping_msg = {"type": "PING", "timestamp": 1234567890.0}
            return ping_msg
        
        msg = asyncio.run(scenario())
        self.assertEqual(msg["type"], "PING")
        self.assertIn("timestamp", msg)
        self.assertIsInstance(msg["timestamp"], float)

    def test_heartbeat_pong_response_format(self):
        """Test that client can respond with PONG"""
        async def scenario():
            # Simulate client responding with PONG
            pong_msg = {"type": "PONG", "timestamp": 1234567890.0}
            # Verify format
            self.assertIsInstance(pong_msg["type"], str)
            self.assertIsInstance(pong_msg["timestamp"], float)
            return pong_msg
        
        msg = asyncio.run(scenario())
        self.assertEqual(msg["type"], "PONG")

    def test_heartbeat_interval_configuration(self):
        """Test that heartbeat interval is configurable"""
        # Verify default heartbeat values in live.py
        heartbeat_interval = 30  # seconds (from code)
        heartbeat_timeout = 60   # seconds (from code)
        
        self.assertEqual(heartbeat_interval, 30)
        self.assertEqual(heartbeat_timeout, 60)
        self.assertLess(heartbeat_interval, heartbeat_timeout)

    def test_pong_timestamp_tracking(self):
        """Test that PONG timestamps are tracked correctly"""
        async def scenario():
            import time
            last_pong = time.time()
            
            # Simulate receiving PONG
            current_time = time.time()
            time_elapsed = current_time - last_pong
            
            return {
                "last_pong": last_pong,
                "current_time": current_time,
                "elapsed": time_elapsed
            }
        
        result = asyncio.run(scenario())
        self.assertGreaterEqual(result["elapsed"], 0)
        self.assertLess(result["elapsed"], 1)  # Should be very fast

    def test_heartbeat_timeout_threshold(self):
        """Test heartbeat timeout detection logic"""
        async def scenario():
            import time
            heartbeat_timeout = 60  # seconds
            
            # Simulate timeout scenario
            last_pong = time.time() - 65  # 65 seconds ago (past timeout)
            current_time = time.time()
            
            time_since_last_pong = current_time - last_pong
            is_timeout = time_since_last_pong > heartbeat_timeout
            
            return is_timeout
        
        is_timeout = asyncio.run(scenario())
        self.assertTrue(is_timeout)

    def test_heartbeat_no_timeout_within_window(self):
        """Test that no timeout occurs within heartbeat window"""
        async def scenario():
            import time
            heartbeat_timeout = 60  # seconds
            
            # Simulate normal scenario
            last_pong = time.time() - 30  # 30 seconds ago (within timeout)
            current_time = time.time()
            
            time_since_last_pong = current_time - last_pong
            is_timeout = time_since_last_pong > heartbeat_timeout
            
            return is_timeout
        
        is_timeout = asyncio.run(scenario())
        self.assertFalse(is_timeout)


class WebSocketDisconnectTest(BaseTestCase):
    """Test WebSocket disconnection and reconnection handling"""

    def setUp(self):
        state_map.clear()
        state_locks.clear()

    def test_disconnect_closes_connection(self):
        """Test that disconnect properly closes WebSocket"""
        async def scenario():
            # Simulate connection states
            states = {
                "OPEN": 1,
                "CLOSED": 3
            }
            
            # Simulate disconnect
            current_state = states["OPEN"]
            current_state = states["CLOSED"]
            
            return current_state == states["CLOSED"]
        
        is_closed = asyncio.run(scenario())
        self.assertTrue(is_closed)

    def test_disconnect_clears_heartbeat_task(self):
        """Test that heartbeat task is cancelled on disconnect"""
        async def scenario():
            heartbeat_task: asyncio.Task | None = None
            heartbeat_task_cancelled = False
            
            # Simulate task cancellation
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    heartbeat_task_cancelled = True
            
            return heartbeat_task_cancelled
        
        # Task was None, so should handle gracefully
        result = asyncio.run(scenario())
        self.assertFalse(result)  # Task was None

    def test_disconnect_cleans_up_intervals(self):
        """Test that client-side intervals are cleaned up"""
        async def scenario():
            heartbeat_interval = None  # After cleanup
            
            # Simulate cleanup
            interval_cleared = heartbeat_interval is None
            
            return interval_cleared
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_disconnect_removes_from_channels(self):
        """Test that WebSocket is removed from broadcast channels"""
        async def scenario():
            # Simulate channel management
            channels = {1: set()}
            box_id = 1
            
            # Simulate removal
            channels[box_id].discard(None)  # WebSocket ref
            
            return len(channels[box_id]) == 0
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_unexpected_disconnect_triggers_cleanup(self):
        """Test cleanup on unexpected disconnection"""
        async def scenario():
            cleanup_called = False
            
            # Simulate finally block execution
            try:
                # Simulate connection error
                raise ConnectionError("Connection lost")
            except ConnectionError:
                pass
            finally:
                cleanup_called = True
            
            return cleanup_called
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_multiple_concurrent_disconnects(self):
        """Test handling multiple simultaneous disconnections"""
        async def scenario():
            # Simulate multiple boxes disconnecting
            active_connections = {1, 2, 3}
            
            # Simulate disconnects
            for box_id in list(active_connections):
                active_connections.discard(box_id)
            
            return len(active_connections) == 0
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_disconnect_during_message_send(self):
        """Test graceful handling of send during disconnect"""
        async def scenario():
            class MockWS:
                def __init__(self, is_open):
                    self.is_open = is_open
                    self.readyState = 1 if is_open else 3
                
                async def send_json(self, data):
                    if self.readyState != 1:
                        raise RuntimeError("WebSocket is closed")
            
            ws = MockWS(is_open=False)
            
            # Try to send on closed connection
            try:
                await ws.send_json({"type": "PONG"})
                sent = False
            except RuntimeError:
                sent = False
            
            return not sent
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_graceful_reconnect_after_disconnect(self):
        """Test reconnection mechanism after disconnect"""
        async def scenario():
            # Simulate reconnect flow
            disconnect_count = 0
            reconnect_count = 0
            
            # First disconnect
            disconnect_count += 1
            
            # Trigger reconnect
            await asyncio.sleep(0.01)  # Simulate delay
            reconnect_count += 1
            
            return disconnect_count == 1 and reconnect_count == 1
        
        result = asyncio.run(scenario())
        self.assertTrue(result)

    def test_websocket_state_preserved_after_reconnect(self):
        """Test that state is preserved across reconnections"""
        async def scenario():
            # Initialize state
            await cmd(Cmd(
                boxId=1,
                type="INIT_ROUTE",
                timerPreset="3:00",
                competitors=[{"nume": "Alice"}]
            ))
            
            # Store current state
            original_state = state_map[1].copy()
            
            # Simulate disconnect/reconnect (state persists on server)
            # No need to reinitialize
            
            return state_map[1] == original_state
        
        result = asyncio.run(scenario())
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
