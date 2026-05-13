"""Tests for POST /api/missions/guid/<guid>/attach_content (recovery endpoint)."""

import datetime
import json
import uuid

import pytest

from opentakserver.extensions import db
from opentakserver.models.Mission import Mission
from opentakserver.models.MissionChange import MissionChange
from opentakserver.models.MissionContent import MissionContent
from opentakserver.models.MissionContentMission import MissionContentMission


@pytest.fixture
def orphan_content(app):
    """Seed a Mission + two unlinked MissionContent rows. Cleans up afterwards."""
    mission_name = f"AttachTestMission-{uuid.uuid4().hex[:8]}"
    mission_guid = str(uuid.uuid4())
    hash_a = "a" * 64
    hash_b = "b" * 64

    with app.app_context():
        mission = Mission()
        mission.name = mission_name
        mission.guid = mission_guid
        mission.create_time = datetime.datetime.now(datetime.timezone.utc)
        mission.tool = "public"
        db.session.add(mission)

        for h in (hash_a, hash_b):
            mc = MissionContent()
            mc.hash = h
            mc.uid = str(uuid.uuid4())
            mc.filename = f"{h[:8]}.kml"
            mc.mime_type = "application/vnd.google-earth.kml+xml"
            mc.size = 100
            mc.expiration = -1
            mc.submission_time = datetime.datetime.now(datetime.timezone.utc)
            mc.submitter = "tester"
            mc.creator_uid = "TEST-EUD"
            mc.keywords = []
            db.session.add(mc)

        db.session.commit()

    yield {"mission_name": mission_name, "mission_guid": mission_guid, "hashes": [hash_a, hash_b]}

    with app.app_context():
        db.session.execute(
            db.delete(MissionChange).filter_by(mission_name=mission_name)
        )
        db.session.execute(
            db.delete(MissionContentMission).filter_by(mission_name=mission_name)
        )
        db.session.execute(
            db.delete(MissionContent).filter(MissionContent.hash.in_([hash_a, hash_b]))
        )
        db.session.execute(db.delete(Mission).filter_by(name=mission_name))
        db.session.commit()


def _silence_pika(monkeypatch):
    """Replace pika so the test doesn't hit a real RabbitMQ."""
    import pika as real_pika

    class _Chan:
        def basic_publish(self, *a, **kw):
            return None

        def close(self):
            return None

    class _Conn:
        def channel(self):
            return _Chan()

        def close(self):
            return None

    monkeypatch.setattr(
        real_pika, "BlockingConnection", lambda *a, **kw: _Conn()
    )


def test_attach_content_rejects_bad_body(auth):
    response = auth.client.post(
        "/api/missions/guid/00000000-0000-0000-0000-000000000000/attach_content",
        headers=auth.headers,
        json={"not_hashes": []},
    )
    assert response.status_code == 400


def test_attach_content_unknown_mission(auth):
    response = auth.client.post(
        "/api/missions/guid/00000000-0000-0000-0000-000000000000/attach_content",
        headers=auth.headers,
        json={"hashes": ["a" * 64]},
    )
    assert response.status_code == 404


def test_attach_content_unknown_hash(auth, orphan_content):
    response = auth.client.post(
        f"/api/missions/guid/{orphan_content['mission_guid']}/attach_content",
        headers=auth.headers,
        json={"hashes": ["deadbeef" * 8]},
    )
    assert response.status_code == 404
    assert "deadbeef" in json.dumps(response.json)


def test_attach_content_happy_path(app, auth, orphan_content, monkeypatch):
    _silence_pika(monkeypatch)

    response = auth.client.post(
        f"/api/missions/guid/{orphan_content['mission_guid']}/attach_content",
        headers=auth.headers,
        json={"hashes": orphan_content["hashes"]},
    )
    assert response.status_code == 200, response.data
    body = response.json
    assert body["success"] is True
    assert body["mission_guid"] == orphan_content["mission_guid"]
    assert len(body["attached"]) == 2
    assert body["already_attached"] == []

    with app.app_context():
        links = db.session.execute(
            db.session.query(MissionContentMission).filter_by(
                mission_name=orphan_content["mission_name"]
            )
        ).scalars().all()
        assert len(links) == 2
        assert all(link.mission_guid == orphan_content["mission_guid"] for link in links)

        changes = db.session.execute(
            db.session.query(MissionChange).filter_by(
                mission_name=orphan_content["mission_name"],
                change_type=MissionChange.ADD_CONTENT,
            )
        ).scalars().all()
        assert len(changes) == 2


def test_attach_content_is_idempotent(app, auth, orphan_content, monkeypatch):
    _silence_pika(monkeypatch)

    url = f"/api/missions/guid/{orphan_content['mission_guid']}/attach_content"
    auth.client.post(url, headers=auth.headers, json={"hashes": orphan_content["hashes"]})
    response = auth.client.post(
        url, headers=auth.headers, json={"hashes": orphan_content["hashes"]}
    )
    assert response.status_code == 200
    assert response.json["attached"] == []
    assert len(response.json["already_attached"]) == 2

    with app.app_context():
        links = db.session.execute(
            db.session.query(MissionContentMission).filter_by(
                mission_name=orphan_content["mission_name"]
            )
        ).scalars().all()
        assert len(links) == 2
