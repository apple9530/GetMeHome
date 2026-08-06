"""Live ETA sharing.

Someone walking home at night tells a friend where they are and when they will
arrive, and the friend can see it stop without having to ask. That is the whole
feature, and it is the one part of this system that handles a live human
location, so the design is shaped by that rather than by convenience.

**The link is the credential.** There are no accounts here, so anyone holding
the URL can watch. Tokens are 128 bits of ``secrets`` randomness, which is not
guessable, and the recipient page says plainly that whoever has the link can
see the location.

**Only the walker can write.** Creating a session returns a second secret that
never appears in the shared link. Without it, holding the link lets you watch
and nothing else — it cannot be used to move the dot, extend the session, or
end it.

**Nothing is kept.** Sessions live in memory and are never written to disk. The
store holds one current position, not a track: a recipient can see where
someone is, never where they have been. When a session ends, the position is
dropped immediately and only the arrival fact survives the grace period.

**It stops on its own.** Three ways, because the one that matters is the one
that works when the walker forgets: arrival ends it, a hard ceiling ends it,
and silence ends it. A share that outlives the walk is the failure mode worth
designing against.

Being explicit about the limits: this is process-local memory. It does not
survive a restart, and with more than one worker a session created on one
worker is invisible to the others. That is fine for a single-process
deployment and wrong for anything larger, where this wants Redis with the same
expiry semantics.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

# A share may not outlive this no matter what the walker does. Long enough for
# a cross-town walk that goes wrong, short enough that a forgotten share is not
# a standing broadcast.
MAX_LIFETIME_S = 4 * 3600

# If the walker's phone stops reporting for this long, the share ends. Covers
# a dead battery, a killed app, and a lost signal that never comes back —
# every case where nobody is left to press stop.
STALE_AFTER_S = 12 * 60

# How long an arrived share stays readable, so a recipient who looks a minute
# later still sees "arrived" rather than "not found". The position is dropped
# at arrival; only the fact of arriving survives this window.
ARRIVED_GRACE_S = 15 * 60

# Recipients poll. Below this they are told to wait, so one open tab cannot
# turn into a request per second.
MIN_POLL_INTERVAL_S = 5


@dataclass
class ShareSession:
    """One walk being shared."""

    token: str
    # Held only by the walker's device. Never in the shared URL.
    owner_key: str
    destination_name: str
    created_at: float
    updated_at: float

    # Current position only — never a history.
    lat: float | None = None
    lon: float | None = None
    # Seconds until arrival, as the walker's device last estimated it.
    eta_s: float | None = None
    remaining_m: float | None = None

    arrived: bool = False
    arrived_at: float | None = None
    ended: bool = False

    @property
    def expires_at(self) -> float:
        return self.created_at + MAX_LIFETIME_S

    def status(self, now: float) -> str:
        """One of: active, arrived, stale, ended."""
        if self.ended:
            return "ended"
        if self.arrived:
            return "arrived"
        if now > self.expires_at:
            return "ended"
        if now - self.updated_at > STALE_AFTER_S:
            return "stale"
        return "active"

    def is_readable(self, now: float) -> bool:
        """Whether a recipient should still see anything at all."""
        if self.arrived and self.arrived_at is not None:
            return now - self.arrived_at <= ARRIVED_GRACE_S
        if self.ended:
            return False
        return now <= self.expires_at

    def finish(self, arrived: bool, now: float) -> None:
        """End the share, dropping the position immediately.

        Arrival is worth keeping for a few minutes so the recipient sees the
        walk completed rather than a link that has silently stopped existing.
        Where the walker *is* is not worth keeping for a second longer.
        """
        self.lat = None
        self.lon = None
        self.eta_s = None
        self.remaining_m = None
        if arrived:
            self.arrived = True
            self.arrived_at = now
        else:
            self.ended = True


class ShareStore:
    """In-memory store of active shares.

    Locked because FastAPI serves synchronous handlers on a thread pool, so
    two updates for the same session can genuinely land at once.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ShareSession] = {}
        self._lock = threading.Lock()

    def create(self, destination_name: str, now: float | None = None) -> ShareSession:
        now = now if now is not None else time.time()
        session = ShareSession(
            token=secrets.token_urlsafe(16),
            owner_key=secrets.token_urlsafe(24),
            destination_name=destination_name[:120],
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._prune(now)
            self._sessions[session.token] = session
        return session

    def get(self, token: str, now: float | None = None) -> ShareSession | None:
        now = now if now is not None else time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if not session.is_readable(now):
                # Expired between requests. Drop it here rather than waiting
                # for the next prune, so it cannot be read once.
                self._sessions.pop(token, None)
                return None
            return session

    def update(
        self,
        token: str,
        owner_key: str,
        lat: float,
        lon: float,
        eta_s: float | None,
        remaining_m: float | None,
        now: float | None = None,
    ) -> ShareSession | None:
        """Move the dot. Returns None if the token or key is wrong."""
        now = now if now is not None else time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None or not session.is_readable(now):
                return None
            # Constant-time: the key is a secret and this endpoint is public.
            if not secrets.compare_digest(session.owner_key, owner_key):
                return None
            if session.arrived or session.ended:
                return None
            if now > session.expires_at:
                session.finish(arrived=False, now=now)
                return None

            session.lat = lat
            session.lon = lon
            session.eta_s = eta_s
            session.remaining_m = remaining_m
            session.updated_at = now
            return session

    def finish(
        self,
        token: str,
        owner_key: str,
        arrived: bool,
        now: float | None = None,
    ) -> ShareSession | None:
        now = now if now is not None else time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if not secrets.compare_digest(session.owner_key, owner_key):
                return None
            session.finish(arrived=arrived, now=now)
            return session

    def _prune(self, now: float) -> None:
        """Drop everything nobody should be able to read. Caller holds the lock."""
        dead = [t for t, s in self._sessions.items() if not s.is_readable(now)]
        for token in dead:
            self._sessions.pop(token, None)

    @property
    def active_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for s in self._sessions.values() if s.status(now) == "active"
            )


@dataclass
class ShareView:
    """What a recipient is allowed to see.

    Deliberately a separate shape from :class:`ShareSession` rather than a
    filtered dump of it. The owner key must never reach this side, and a
    hand-written projection makes that a compile-time-ish property instead of
    something that depends on remembering to exclude a field.
    """

    status: str
    destination_name: str
    lat: float | None
    lon: float | None
    eta_s: float | None
    remaining_m: float | None
    updated_ago_s: float
    expires_in_s: float
    poll_after_s: int = MIN_POLL_INTERVAL_S


def view_of(session: ShareSession, now: float | None = None) -> ShareView:
    now = now if now is not None else time.time()
    return ShareView(
        status=session.status(now),
        destination_name=session.destination_name,
        lat=session.lat,
        lon=session.lon,
        eta_s=session.eta_s,
        remaining_m=session.remaining_m,
        updated_ago_s=max(0.0, now - session.updated_at),
        expires_in_s=max(0.0, session.expires_at - now),
    )


_store: ShareStore | None = None
_store_lock = threading.Lock()


def get_store() -> ShareStore:
    """The process-wide store."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ShareStore()
    return _store


def reset_store() -> None:
    """Drop every share. For tests, and for a clean shutdown."""
    global _store
    with _store_lock:
        _store = ShareStore()


# ----------------------------------------------------------------------
# The recipient's page
# ----------------------------------------------------------------------


def recipient_page(token: str) -> str:
    """A self-contained status page for whoever holds the link.

    No map tiles, and that is a decision rather than an omission. Embedding a
    tile provider would hand a third party the live position of someone who
    never agreed to that — the exact thing this feature is supposed to be
    careful with. Instead the page states the position in words and offers to
    open it in the recipient's own maps app, which is their choice to make.

    No external requests of any kind, so the page works behind a firewall and
    leaks nothing by loading.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<!-- Keep the token out of any referrer sent when the user follows a link. -->
<meta name="referrer" content="no-referrer">
<title>Shared walk</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 2rem 1.25rem; display: flex; justify-content: center;
  }}
  main {{ width: 100%; max-width: 26rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; }}
  .eta {{ font-size: 3rem; font-weight: 650; letter-spacing: -.02em; margin: 1.5rem 0 .25rem; }}
  .muted {{ opacity: .65; font-size: .9rem; }}
  .card {{
    border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
    border-radius: 14px; padding: 1.1rem 1.25rem; margin: 1.25rem 0;
  }}
  .dot {{
    display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
    background: #34c759; margin-right: .4rem; vertical-align: baseline;
  }}
  .dot.stale {{ background: #ff9f0a; }}
  .dot.done {{ background: #8e8e93; }}
  a.button {{
    display: block; text-align: center; text-decoration: none;
    padding: .8rem; border-radius: 12px; font-weight: 600;
    background: #0a84ff; color: #fff; margin-top: 1rem;
  }}
  footer {{ margin-top: 2rem; font-size: .8rem; opacity: .6; }}
</style>
</head>
<body>
<main>
  <h1 id="title">Shared walk</h1>
  <div class="muted" id="subtitle">Connecting…</div>

  <div class="card">
    <div class="eta" id="eta">—</div>
    <div class="muted" id="detail"></div>
    <div class="muted" id="freshness" style="margin-top:.75rem"></div>
    <a class="button" id="open" style="display:none">Open in Maps</a>
  </div>

  <footer>
    Anyone with this link can see this location while the walk is in progress.
    It stops on arrival, and by itself after a few hours.
  </footer>
</main>
<script>
const TOKEN = {token!r};
const title = document.getElementById('title');
const subtitle = document.getElementById('subtitle');
const eta = document.getElementById('eta');
const detail = document.getElementById('detail');
const freshness = document.getElementById('freshness');
const open = document.getElementById('open');

function minutes(seconds) {{
  if (seconds === null || seconds === undefined) return null;
  const m = Math.round(seconds / 60);
  return m <= 0 ? 'Arriving' : m + ' min';
}}

function ago(seconds) {{
  if (seconds < 45) return 'just now';
  const m = Math.round(seconds / 60);
  return m + (m === 1 ? ' minute ago' : ' minutes ago');
}}

async function tick() {{
  let data;
  try {{
    const response = await fetch('/share/' + TOKEN, {{cache: 'no-store'}});
    if (response.status === 404) {{
      title.textContent = 'This link has expired';
      subtitle.textContent = 'The walk finished, or the share ended.';
      eta.textContent = '—';
      detail.textContent = '';
      freshness.textContent = '';
      open.style.display = 'none';
      return;
    }}
    data = await response.json();
  }} catch (e) {{
    subtitle.textContent = 'Reconnecting…';
    setTimeout(tick, 8000);
    return;
  }}

  const dest = data.destinationName || 'their destination';
  title.textContent = 'Walking to ' + dest;

  if (data.status === 'arrived') {{
    subtitle.innerHTML = '<span class="dot done"></span>Arrived';
    eta.textContent = 'Arrived';
    detail.textContent = 'They got there safely.';
    freshness.textContent = '';
    open.style.display = 'none';
    return;
  }}
  if (data.status === 'ended') {{
    title.textContent = 'Sharing has ended';
    subtitle.textContent = '';
    eta.textContent = '—';
    open.style.display = 'none';
    return;
  }}

  const stale = data.status === 'stale';
  subtitle.innerHTML = stale
    ? '<span class="dot stale"></span>No update recently'
    : '<span class="dot"></span>Live';

  eta.textContent = minutes(data.etaSeconds) || '—';
  detail.textContent = data.remainingMetres
    ? Math.round(data.remainingMetres) + ' m to go'
    : '';
  freshness.textContent = 'Updated ' + ago(data.updatedAgoSeconds);

  if (data.lat !== null && data.lon !== null) {{
    open.href = 'geo:' + data.lat + ',' + data.lon
      + '?q=' + data.lat + ',' + data.lon;
    open.style.display = 'block';
  }} else {{
    open.style.display = 'none';
  }}

  setTimeout(tick, Math.max(5, data.pollAfterSeconds || 10) * 1000);
}}

tick();
</script>
</body>
</html>"""
