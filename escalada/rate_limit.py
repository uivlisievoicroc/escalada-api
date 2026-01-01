"""
Rate limiting implementation using in-memory tracking
Prevents DoS attacks on /api/cmd endpoint
"""

import logging
import time
from collections import defaultdict
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Per-box and per-command-type rate limiter
    Tracks requests in memory with automatic cleanup
    """

    def __init__(
        self,
        max_per_minute: int = 300,
        max_per_second: int = 20,
        block_duration: int = 60,
    ):
        """
        Initialize rate limiter

        Args:
            max_per_minute: Max requests per box per minute
            max_per_second: Max requests per box per second
            block_duration: How long to block after limit (seconds)
        """
        self.max_per_minute = max_per_minute
        self.max_per_second = max_per_second
        self.block_duration = block_duration

        # Track requests: { boxId: { 'requests': [timestamp, ...], 'blocked_until': time } }
        self.request_history: Dict[int, Dict] = defaultdict(
            lambda: {"requests": [], "blocked_until": 0}
        )

        # Per-command limits: { boxId: { command_type: [timestamp, ...] } }
        self.command_history: Dict[int, Dict[str, list]] = defaultdict(
            lambda: defaultdict(list)
        )

        # Custom per-command limits
        self.command_limits: Dict[str, int] = {}

    def set_command_limit(self, command_type: str, max_per_minute: int):
        """Set custom limit for specific command type"""
        self.command_limits[command_type] = max_per_minute

    def reset_all(self):
        """Reset all rate limiting data (for testing)"""
        self.request_history = defaultdict(lambda: {"requests": [], "blocked_until": 0})
        self.command_history = defaultdict(lambda: defaultdict(list))

    def is_blocked(self, box_id: int) -> bool:
        """Check if box is currently blocked"""
        blocked_until = self.request_history[box_id]["blocked_until"]
        if blocked_until > time.time():
            logger.warning(f"Box {box_id} is rate-limited until {blocked_until}")
            return True
        return False

    def check_rate_limit(self, box_id: int, command_type: str) -> Tuple[bool, str]:
        """
        Check if request should be rate-limited

        Args:
            box_id: The box ID
            command_type: The command type

        Returns:
            Tuple[bool, str]: (is_allowed, reason)
                is_allowed: True if request is allowed
                reason: Reason if blocked (empty string if allowed)
        """
        current_time = time.time()

        # Check if box is blocked
        if self.is_blocked(box_id):
            return False, f"Box {box_id} is rate-limited. Try again later."

        # Get history for this box
        history = self.request_history[box_id]
        requests = history["requests"]

        # Remove old requests (older than 1 minute)
        requests[:] = [ts for ts in requests if current_time - ts < 60]

        # Check per-second limit
        recent_requests = [ts for ts in requests if current_time - ts < 1]
        if len(recent_requests) >= self.max_per_second:
            # Block this box
            history["blocked_until"] = current_time + self.block_duration
            logger.warning(
                f"Box {box_id} exceeded per-second limit ({self.max_per_second} req/sec)"
            )
            return False, f"Rate limit exceeded (too many requests per second)"

        # Check per-minute limit
        if len(requests) >= self.max_per_minute:
            history["blocked_until"] = current_time + self.block_duration
            logger.warning(
                f"Box {box_id} exceeded per-minute limit ({self.max_per_minute} req/min)"
            )
            return False, f"Rate limit exceeded (too many requests per minute)"

        # Check command-specific limits
        cmd_limit = self.command_limits.get(
            command_type, 999
        )  # Default: very permissive
        cmd_requests = self.command_history[box_id][command_type]

        # Remove old command requests
        cmd_requests[:] = [ts for ts in cmd_requests if current_time - ts < 60]

        if len(cmd_requests) >= cmd_limit:
            logger.warning(
                f"Box {box_id} exceeded {command_type} limit ({cmd_limit} per minute)"
            )
            return False, f"Rate limit exceeded for {command_type} command"

        # Record this request
        requests.append(current_time)
        cmd_requests.append(current_time)

        return True, ""

    def cleanup_old_data(self, max_age_seconds: int = 300):
        """Remove old data to prevent memory buildup (call periodically)"""
        current_time = time.time()
        cutoff_time = current_time - max_age_seconds

        # Clean request history
        to_delete = []
        for box_id, history in self.request_history.items():
            history["requests"][:] = [
                ts for ts in history["requests"] if ts > cutoff_time
            ]
            # Delete empty entries
            if not history["requests"] and history["blocked_until"] < current_time:
                to_delete.append(box_id)

        for box_id in to_delete:
            del self.request_history[box_id]

        # Clean command history
        cmd_to_delete = []
        for box_id, commands in self.command_history.items():
            for cmd_type, requests in commands.items():
                commands[cmd_type] = [ts for ts in requests if ts > cutoff_time]
            if not commands:
                cmd_to_delete.append(box_id)

        for box_id in cmd_to_delete:
            del self.command_history[box_id]

    def get_stats(self, box_id: int) -> dict:
        """Get rate limit stats for debugging"""
        current_time = time.time()
        history = self.request_history[box_id]
        requests = history["requests"]

        # Count recent requests
        recent_1sec = len([ts for ts in requests if current_time - ts < 1])
        recent_1min = len([ts for ts in requests if current_time - ts < 60])

        # Count per-command
        command_counts = {}
        for cmd_type, cmd_requests in self.command_history[box_id].items():
            command_counts[cmd_type] = len(
                [ts for ts in cmd_requests if current_time - ts < 60]
            )

        return {
            "requests_per_second": recent_1sec,
            "requests_per_minute": recent_1min,
            "is_blocked": self.is_blocked(box_id),
            "blocked_until": history["blocked_until"],
            "command_counts": command_counts,
        }


# Global rate limiter instance
_rate_limiter = None


def get_rate_limiter() -> RateLimiter:
    """Get or create global rate limiter"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(
            max_per_minute=300, max_per_second=20, block_duration=60
        )

        # Set command-specific limits
        _rate_limiter.set_command_limit("PROGRESS_UPDATE", 120)  # Frequent
        _rate_limiter.set_command_limit("INIT_ROUTE", 10)  # Rare
        _rate_limiter.set_command_limit("SUBMIT_SCORE", 30)  # Occasional
        _rate_limiter.set_command_limit(
            "REGISTER_TIME", 300
        )  # Allow frequent timestamp saves

    return _rate_limiter


def check_rate_limit(box_id: int, command_type: str) -> Tuple[bool, str]:
    """Convenience function to check rate limit"""
    limiter = get_rate_limiter()
    return limiter.check_rate_limit(box_id, command_type)


def cleanup_rate_limit_data():
    """Cleanup old rate limiting data (call periodically, e.g., every 5 minutes)"""
    limiter = get_rate_limiter()
    limiter.cleanup_old_data()


__all__ = [
    "RateLimiter",
    "get_rate_limiter",
    "check_rate_limit",
    "cleanup_rate_limit_data",
]
