"""Live ETA sharing.

Most of this file is about what a share must *not* do. It is the only part of
the service that handles a live human location, and the failure modes that
matter are not crashes — they are a share that outlives the walk, a link that
turns out to grant more than watching, and a position that survives being
stopped.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from getmehome.api.main import app
from getmehome.sharing import (
    ARRIVED_GRACE_S,
    MAX_LIFETIME_S,
    STALE_AFTER_S,
    ShareStore,
    get_store,
    reset_store,
    view_of,
)

NOW = 1_800_000_000.0


@pytest.fixture
def store() -> ShareStore:
    return ShareStore()


# ---------------------------------------------------------------------------
# Reading and writing are separate
# ---------------------------------------------------------------------------


def test_the_link_alone_cannot_move_the_dot(store):
    """The whole point of a second secret.

    A recipient necessarily holds the token. If that were enough to write,
    anyone the walker shared with could fake their position or end the share.
    """
    session = store.create("home", now=NOW)

    assert store.update(session.token, "", 38.9, -77.0, 600, 800, now=NOW) is None
    assert (
        store.update(session.token, "guessed-key", 38.9, -77.0, 600, 800, now=NOW)
        is None
    )
    assert store.get(session.token, now=NOW).lat is None


def test_the_owner_key_moves_it(store):
    session = store.create("home", now=NOW)
    updated = store.update(
        session.token, session.owner_key, 38.91, -77.03, 540, 700, now=NOW + 10
    )

    assert updated is not None
    assert (updated.lat, updated.lon) == (38.91, -77.03)
    assert updated.eta_s == 540


def test_the_recipient_view_never_carries_the_owner_key(store):
    session = store.create("home", now=NOW)
    store.update(session.token, session.owner_key, 38.9, -77.0, 600, 800, now=NOW)

    view = view_of(store.get(session.token, now=NOW), now=NOW)
    assert session.owner_key not in repr(view)
    assert not hasattr(view, "owner_key")


def test_only_the_owner_can_end_a_share(store):
    session = store.create("home", now=NOW)
    assert store.finish(session.token, "wrong", arrived=True, now=NOW) is None
    assert store.get(session.token, now=NOW) is not None


def test_tokens_are_not_guessable(store):
    tokens = {store.create("home", now=NOW).token for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 20 for t in tokens)


def test_the_owner_key_is_not_the_token(store):
    session = store.create("home", now=NOW)
    assert session.owner_key != session.token


# ---------------------------------------------------------------------------
# It stops on its own
# ---------------------------------------------------------------------------


def test_a_share_dies_at_the_hard_ceiling(store):
    """The case that matters: the walker forgot to stop it."""
    session = store.create("home", now=NOW)
    store.update(session.token, session.owner_key, 38.9, -77.0, 600, 800, now=NOW)

    assert store.get(session.token, now=NOW + MAX_LIFETIME_S - 60) is not None
    assert store.get(session.token, now=NOW + MAX_LIFETIME_S + 1) is None


def test_an_expired_share_cannot_be_kept_alive_by_updating_it(store):
    """Otherwise the ceiling is not a ceiling."""
    session = store.create("home", now=NOW)
    late = NOW + MAX_LIFETIME_S + 1

    assert store.update(session.token, session.owner_key, 38.9, -77.0, 1, 1, late) is None
    assert store.get(session.token, now=late) is None


def test_silence_marks_a_share_stale(store):
    """A dead battery leaves nobody to press stop."""
    session = store.create("home", now=NOW)
    store.update(session.token, session.owner_key, 38.9, -77.0, 600, 800, now=NOW)

    assert store.get(session.token, now=NOW + 60).status(NOW + 60) == "active"
    quiet = NOW + STALE_AFTER_S + 1
    assert store.get(session.token, now=quiet).status(quiet) == "stale"


def test_arrival_ends_it_and_drops_the_position(store):
    """Where they arrived is not worth keeping for a second longer."""
    session = store.create("home", now=NOW)
    store.update(session.token, session.owner_key, 38.9, -77.0, 60, 90, now=NOW)
    store.finish(session.token, session.owner_key, arrived=True, now=NOW + 100)

    after = store.get(session.token, now=NOW + 200)
    assert after is not None
    assert after.status(NOW + 200) == "arrived"
    assert after.lat is None and after.lon is None
    assert after.eta_s is None


def test_an_arrived_share_stays_readable_only_briefly(store):
    """Long enough to see "arrived", not long enough to be a record."""
    session = store.create("home", now=NOW)
    store.finish(session.token, session.owner_key, arrived=True, now=NOW)

    assert store.get(session.token, now=NOW + ARRIVED_GRACE_S - 10) is not None
    assert store.get(session.token, now=NOW + ARRIVED_GRACE_S + 10) is None


def test_stopping_early_drops_everything_at_once(store):
    session = store.create("home", now=NOW)
    store.update(session.token, session.owner_key, 38.9, -77.0, 600, 800, now=NOW)
    store.finish(session.token, session.owner_key, arrived=False, now=NOW + 60)

    assert store.get(session.token, now=NOW + 61) is None


def test_a_finished_share_cannot_be_restarted(store):
    session = store.create("home", now=NOW)
    store.finish(session.token, session.owner_key, arrived=True, now=NOW)

    assert (
        store.update(session.token, session.owner_key, 38.9, -77.0, 1, 1, NOW + 10)
        is None
    )


# ---------------------------------------------------------------------------
# Only the current position, never a track
# ---------------------------------------------------------------------------


def test_only_the_latest_position_is_held(store):
    """A recipient sees where someone is, never where they have been."""
    session = store.create("home", now=NOW)
    for i in range(5):
        store.update(
            session.token, session.owner_key,
            38.90 + i * 0.001, -77.00, 600 - i * 60, 800, now=NOW + i * 30,
        )

    current = store.get(session.token, now=NOW + 200)
    assert current.lat == pytest.approx(38.904)
    # Nothing anywhere on the session holds the earlier fixes.
    assert "38.901" not in repr(current)


def test_pruning_removes_dead_sessions(store):
    old = store.create("home", now=NOW)
    store.finish(old.token, old.owner_key, arrived=False, now=NOW)
    store.create("elsewhere", now=NOW + MAX_LIFETIME_S + 100)

    assert store.get(old.token, now=NOW + MAX_LIFETIME_S + 100) is None


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    reset_store()
    with TestClient(app) as c:
        yield c
    reset_store()


def test_the_full_flow_over_http(client):
    created = client.post("/share", json={"destinationName": "Home"}).json()
    assert created["url"].endswith(created["token"])
    assert created["ownerKey"]

    token, key = created["token"], created["ownerKey"]

    moved = client.post(
        f"/share/{token}/update",
        json={"ownerKey": key, "lat": 38.91, "lon": -77.03, "etaSeconds": 480},
    ).json()
    assert moved["status"] == "active"
    assert moved["etaSeconds"] == 480

    watching = client.get(f"/share/{token}").json()
    assert watching["destinationName"] == "Home"
    assert watching["lat"] == 38.91
    assert "ownerKey" not in watching

    ended = client.post(
        f"/share/{token}/end", json={"ownerKey": key, "arrived": True}
    ).json()
    assert ended["status"] == "arrived"
    assert ended["lat"] is None


def test_a_bad_key_and_a_bad_token_answer_identically(client):
    """Distinguishing them would confirm a guessed token exists."""
    created = client.post("/share", json={"destinationName": "Home"}).json()

    wrong_key = client.post(
        f"/share/{created['token']}/update",
        json={"ownerKey": "nope", "lat": 38.9, "lon": -77.0},
    )
    unknown_token = client.post(
        "/share/not-a-real-token/update",
        json={"ownerKey": "nope", "lat": 38.9, "lon": -77.0},
    )

    assert wrong_key.status_code == unknown_token.status_code == 404
    assert wrong_key.json() == unknown_token.json()


def test_an_expired_link_reads_as_expired(client):
    assert client.get("/share/not-a-real-token").status_code == 404


def test_the_recipient_page_is_served_for_any_token(client):
    """A 404 here would let a prober tell a live share from a dead one."""
    live = client.post("/share", json={"destinationName": "Home"}).json()

    for token in (live["token"], "not-a-real-token"):
        page = client.get(f"/s/{token}")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]


def test_the_recipient_page_refuses_to_be_cached_or_indexed(client):
    page = client.get("/s/whatever")
    assert "no-store" in page.headers["cache-control"]
    assert "noindex" in page.headers["x-robots-tag"]
    assert page.headers["referrer-policy"] == "no-referrer"


def test_the_recipient_page_makes_no_external_requests(client):
    """It carries a live location; nothing about it should leave the server.

    No tile provider either. Embedding one would hand a third party the
    position of someone who never agreed to that.
    """
    body = client.get("/s/whatever").text
    for marker in ("http://", "https://", "//cdn", "<img", "@import"):
        assert marker not in body, marker


def test_the_page_says_who_can_see_it(client):
    body = client.get("/s/whatever").text
    assert "Anyone with this link" in body


def test_the_store_is_process_wide(client):
    created = client.post("/share", json={"destinationName": "Home"}).json()
    assert get_store().get(created["token"]) is not None
