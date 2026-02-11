import pytest

import escalada.main as main_module
import escalada.storage.json_store as json_store


def test_main_secret_helpers_detect_weak_values():
    assert main_module._is_weak_jwt_secret(None) is True
    assert main_module._is_weak_jwt_secret("dev-secret-change-me") is True
    assert main_module._is_weak_jwt_secret("strong-secret") is False

    assert main_module._is_weak_default_admin_password(None) is True
    assert main_module._is_weak_default_admin_password("admin") is True
    assert main_module._is_weak_default_admin_password("S3cure!") is False


def test_validate_default_admin_password_fails_in_production_for_default(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "admin")

    with pytest.raises(RuntimeError):
        json_store._validate_default_admin_password()


def test_validate_default_admin_password_allows_strong_value_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "S3cure!")

    assert json_store._validate_default_admin_password() == "S3cure!"
