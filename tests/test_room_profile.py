from unittest.mock import AsyncMock

import pytest
from conftest import Enricher, Factory
from fastapi.testclient import TestClient

from live_service.app import create_app
from live_service.manager import ConnectionManager
from live_service.upstream.bootstrap import normalize_username
from live_service.upstream.errors import UpstreamError


@pytest.mark.parametrize("raw", [".", "..", "creator/suffix", "a" * 65])
def test_rejects_invalid_canonical_usernames(raw):
    with pytest.raises(ValueError):
        normalize_username(raw)


@pytest.mark.parametrize("live", [True, False])
def test_private_profile_requires_key_and_preserves_live_status(settings, monkeypatch, tmp_path, live):
    monkeypatch.setenv("LIVE_INTERNAL_API_KEY", "local-profile-test")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    manager = ConnectionManager(settings, client_factory=Factory(), enricher=Enricher())
    profile = dict(username="creator", roomId="123" if live else "", live=live,
                   nickname="Creator", avatarUrl="https://example.invalid/avatar.png")
    manager.resolve_room_profile = AsyncMock(return_value=profile)
    with TestClient(create_app(manager)) as client:
        assert client.get("/internal/rooms/creator").status_code == 403
        assert client.get("/internal/rooms/creator", headers={"X-Live-Service-Key": "wrong"}).status_code == 403
        manager.resolve_room_profile.assert_not_awaited()
        response = client.get("/internal/rooms/@CREATOR", headers={"X-Live-Service-Key": "local-profile-test"})
        assert response.status_code == 200 and response.json() == profile
        manager.resolve_room_profile.assert_awaited_once_with("creator")
        assert "cookie" not in response.text.lower()


@pytest.mark.parametrize("reason,status", [("user_not_found", 404), ("bootstrap_timeout", 503), ("invalid_response", 503)])
def test_private_profile_returns_explicit_errors(settings, monkeypatch, tmp_path, reason, status):
    monkeypatch.setenv("LIVE_INTERNAL_API_KEY", "local-profile-test")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    manager = ConnectionManager(settings, client_factory=Factory(), enricher=Enricher())
    manager.resolve_room_profile = AsyncMock(side_effect=UpstreamError(reason))
    with TestClient(create_app(manager)) as client:
        response = client.get("/internal/rooms/creator", headers={"X-Live-Service-Key": "local-profile-test"})
        assert response.status_code == status and response.json() == {"detail": reason}
