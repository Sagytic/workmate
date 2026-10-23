"""Companion defaults and user-selected names through the settings API."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.companion import install_companion
from backend.store import Store


@pytest.fixture
def companion_client(tmp_path):
    app = FastAPI()
    install_companion(app, SimpleNamespace(store=Store(tmp_path / 'test.db')))
    with TestClient(app) as client:
        yield client


def test_new_workspace_shows_mini(companion_client):
    response = companion_client.get('/api/companion')
    assert response.status_code == 200
    assert response.json()['preferences']['name'] == '미니'


@pytest.mark.parametrize('name', ['나의 비서', '모아'])
def test_saved_names_and_notification_settings_are_preserved(companion_client, name):
    response = companion_client.patch('/api/companion/preferences', json={
        'name': name, 'quiet': True, 'enabled': False,
        'quiet_until': '2026-12-01T09:00:00+09:00',
    })
    assert response.status_code == 200
    prefs = companion_client.get('/api/companion').json()['preferences']
    assert prefs['name'] == name
    assert prefs['quiet'] is True
    assert prefs['enabled'] is False
    assert prefs['quiet_until'] == '2026-12-01T09:00:00+09:00'
