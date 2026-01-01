"""
Backward-compatible shim: validation moved to `escalada_core.validation`.
Prefer importing from `escalada_core.validation` directly.
"""

from escalada_core.validation import InputSanitizer, RateLimitConfig, ValidatedCmd

__all__ = ["ValidatedCmd", "RateLimitConfig", "InputSanitizer"]

