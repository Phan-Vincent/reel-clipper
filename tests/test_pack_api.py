"""Focused request-shape regression tests for the style-pack API."""
import asyncio

import pytest
from fastapi import HTTPException, Request

from app import main


def _request(body: bytes) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/packs",
                    "headers": []}, receive)


@pytest.mark.parametrize("body", [
    b'{"name":"Broken style","pack":"not-an-object"}',
    b'{"name":"Broken style","pack":[]}',
])
def test_create_pack_rejects_non_object_pack(body):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.create_pack(_request(body)))

    assert caught.value.status_code == 400
    assert caught.value.detail == "'pack' must be a JSON object"


def test_create_pack_accepts_ui_request_shape(monkeypatch):
    received = {}

    def fake_save(pack, name):
        received.update(pack=pack, name=name)
        return {"id": "a" * 32, "name": name, "kind": "saved"}

    monkeypatch.setattr(main.packstore, "save", fake_save)
    body = b'{"name":"Warm reel","pack":{"preset":"cinematic"}}'

    result = asyncio.run(main.create_pack(_request(body)))

    assert received == {"name": "Warm reel", "pack": {"preset": "cinematic"}}
    assert result["pack"]["key"] == "a" * 32


# ---- create_job config parsing (adversarial inputs) --------------------------

def test_client_config_strips_server_managed_paths():
    # clips/music/export_audio are server-managed; a client must not be able
    # to point ffmpeg at arbitrary read/write paths via the config field.
    mode, cfg = main._client_config(
        '{"clips": ["/etc/passwd"], "music": "/etc/shadow", '
        '"export_audio": "/tmp/pwn.m4a", "preset": "clean"}')
    assert mode == "full"
    assert "clips" not in cfg and "music" not in cfg and "export_audio" not in cfg
    assert cfg["preset"] == "clean"


def test_client_config_storyboard_mode():
    mode, cfg = main._client_config('{"storyboard": true, "day": 3}')
    assert mode == "storyboard"
    assert "storyboard" not in cfg and cfg["day"] == 3


@pytest.mark.parametrize("raw", ["not json", "[1,2,3]", '"a string"', "42"])
def test_client_config_rejects_malformed_or_non_object(raw):
    with pytest.raises(HTTPException) as caught:
        main._client_config(raw)
    assert caught.value.status_code == 400
