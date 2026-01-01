# pyright: reportMissingImports=false
"""
Pytest configuration and fixtures for Escalada tests
"""
import sys
import types
from typing import Any


def pytest_configure(config):
    """Configure pytest - only stub if modules aren't installed"""
    
    # Only stub if fastapi isn't actually installed
    try:
        import fastapi
        import fastapi.testclient  # noqa
    except (ImportError, ModuleNotFoundError):
        # Create stubs for optional runtime deps
        fastapi_stub: Any = types.ModuleType("fastapi")
        
        class _DummyRouter:
            def post(self, *args, **kwargs):
                return lambda f: f
            def websocket(self, *args, **kwargs):
                return lambda f: f
            def get(self, *args, **kwargs):
                return lambda f: f
        
        class _HTTPException(Exception):
            def __init__(self, status_code=None, detail=None):
                self.status_code = status_code
                self.detail = detail
                super().__init__(f"HTTPException: {status_code} - {detail}")
        
        fastapi_stub.APIRouter = _DummyRouter
        fastapi_stub.HTTPException = _HTTPException
        sys.modules["fastapi"] = fastapi_stub
    
    # Only stub starlette/websockets if not installed
    try:
        import starlette
        import starlette.websockets  # noqa
    except (ImportError, ModuleNotFoundError):
        starlette_stub: Any = types.ModuleType("starlette")
        websockets_stub: Any = types.ModuleType("starlette.websockets")
        
        class _DummyWebSocket:
            pass
        
        websockets_stub.WebSocket = _DummyWebSocket
        sys.modules["starlette"] = starlette_stub
        sys.modules["starlette.websockets"] = websockets_stub
