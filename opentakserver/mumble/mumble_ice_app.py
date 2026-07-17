import os
import threading
import time
from datetime import datetime, timezone
from threading import Timer

import Ice

from opentakserver.mumble.mumble_authenticator import MumbleAuthenticator

# Load up Murmur slice file into Ice
Ice.loadSlice(
    "",
    [
        "-I" + Ice.getSliceDir(),
        os.path.join(os.path.dirname(os.path.realpath(__file__)), "Murmur.ice"),
    ],
)
import Murmur


# Murmur permission bit masks — names mirror Murmur.ice for grep-ability.
PERM_TRAVERSE = 0x002
PERM_ENTER = 0x004
PERM_SPEAK = 0x008
PERM_WHISPER = 0x100
PERM_TEXT_MESSAGE = 0x200
PERM_MAKE_TEMP_CHANNEL = 0x400

# Baseline speak/text grant used on temp channels so any authenticated user
# (regardless of which OTS group they belong to) can join a VX-initiated
# conference call once invited.
PERM_BASELINE_SPEAK = (
    PERM_TRAVERSE | PERM_ENTER | PERM_SPEAK | PERM_WHISPER | PERM_TEXT_MESSAGE
)

# Same admin grant Murmur uses on Root by default — gives admins full control
# of temp channels (kick stragglers, link, etc.) without touching their existing
# per-channel admin rights.
PERM_ADMIN_FULL = 0x707FF


class MumbleIceDaemon(threading.Thread):
    def __init__(self, app, logger):
        super().__init__()
        self.app = app
        self.logger = logger
        self.logger.info("mumble daemon init")
        self.daemon = True

    def run(self):
        # Configure Ice properties
        props = Ice.createProperties()
        props.setProperty("Ice.ImplicitContext", "Shared")
        props.setProperty("Ice.Default.EncodingVersion", "1.0")
        # 5s is plenty for localhost Ice round-trips and lets transient
        # Murmur stalls surface as fast errors instead of compounding into
        # minute-long client-visible hangs.
        props.setProperty("Ice.Default.InvocationTimeout", str(5 * 1000))
        # Cap the client thread pool so bursts of concurrent Ice calls during
        # call lifecycle can't queue beyond what Murmur can drain.
        props.setProperty("Ice.ThreadPool.Client.SizeMax", "10")
        # Disable Ice's default retry-once behavior.  With the short timeout
        # above we'd rather see a timeout immediately than wait for a retry
        # that doubles user-visible latency.
        props.setProperty("Ice.RetryIntervals", "-1")
        props.setProperty("Ice.MessageSizeMax", str(1024))
        idata = Ice.InitializationData()
        idata.properties = props

        # Create Ice connection.  The secret must be in ImplicitContext
        # before any proxy call (e.g. checkedCast below) or Murmur rejects
        # with InvalidSecretException.
        ice = Ice.initialize(idata)
        secret = self.app.config.get("OTS_ICE_SECRET", "")
        if secret:
            ice.getImplicitContext().put("secret", secret)
        else:
            self.logger.warning(
                "OTS_ICE_SECRET is empty; Murmur will reject Ice calls if its "
                "icesecretread/icesecretwrite is set."
            )

        proxy = ice.stringToProxy("Meta:tcp -h 127.0.0.1 -p 6502")
        try:
            meta = Murmur.MetaPrx.checkedCast(proxy)
        except Ice.ConnectionRefusedException:
            self.logger.error("Failed to connect to the mumble ice server")
            return

        mumble_ice_app = MumbleIceApp(self.app, self.logger, ice)
        mumble_ice_app.run()


class MumbleIceApp(Ice.Application):
    def __init__(self, app, logger, ice):
        super().__init__()
        self.app = app
        self.logger = logger
        self.ice = ice
        self.meta = None
        self.metacb = None
        self.connected = False
        self.failed_watch = False
        self.watchdog = None
        self.auth = None
        self.adapter = None
        # server_id -> ServerCallbackPrx; guards against duplicate registration
        self.server_callbacks = {}
        # channel_id -> last activity datetime (UTC).  Updated by the direction
        # enforcement callback on every userConnected / userStateChanged so the
        # scheduled cleanup job can identify idle event channels.  After a
        # service restart, channels with no entry fall back to service_start_time.
        self._channel_last_active = {}
        self._channel_activity_lock = threading.Lock()
        self.service_start_time = datetime.now(timezone.utc)
        # Serializes ACL get/modify/set passes on persistent channels so
        # concurrent request_sync calls (e.g. multiple group_api add operations)
        # can't TOCTOU-race each other or clobber manual ACL edits made between
        # the read and the write.
        self._acl_lock = threading.Lock()
        # Expose this daemon to Flask blueprints so group_api can request channel
        # syncs after add/delete.  app.extensions is a plain dict; reads are thread-safe.
        self.app.extensions["mumble_ice_app"] = self

    def record_channel_activity(self, channel_id):
        """Stamp `now` as the last-active time for a channel.  Called by the
        direction-enforcement callback on connect / channel-hop, and by the
        cleanup job when it observes a channel that's currently occupied."""
        with self._channel_activity_lock:
            self._channel_last_active[channel_id] = datetime.now(timezone.utc)

    def get_channel_last_active(self, channel_id):
        """Return the last-activity timestamp for a channel, falling back to
        service_start_time if no activity has been recorded since boot."""
        with self._channel_activity_lock:
            return self._channel_last_active.get(channel_id, self.service_start_time)

    def forget_channel_activity(self, channel_id):
        """Drop a channel's last-active entry (e.g. after the cleanup job
        deletes the channel) so the dict doesn't grow unboundedly."""
        with self._channel_activity_lock:
            self._channel_last_active.pop(channel_id, None)

    def run(self, *args):
        if not self.initialize_ice_connection():
            self.logger.error("Mumble server connection failed")
            return 1

        # check_connection() schedules a recursive 10s Timer that reattaches
        # callbacks if Murmur restarts.  Don't cancel it -- leaving it running
        # is the whole point.  Without it, OTS doesn't recover after a Murmur
        # restart and clients get "Wrong certificate or password" rejections
        # until OTS is manually restarted.
        self.check_connection()

        if self.interrupted():
            self.logger.warning("Caught interrupt, shutting down")

        return 0

    def initialize_ice_connection(self):
        """
        Establishes the two-way Ice connection and adds the authenticator to the
        configured servers.  The Ice secret was already pushed into the shared
        ImplicitContext by MumbleIceDaemon.run().
        """

        self.logger.debug("Connecting to Ice server ({}:{})".format("127.0.0.1", 6502))
        base = self.ice.stringToProxy("Meta:tcp -h {} -p {}".format("127.0.0.1", 6502))
        self.meta = Murmur.MetaPrx.uncheckedCast(base)

        adapter = self.ice.createObjectAdapterWithEndpoints("Callback.Client", "tcp -h 127.0.0.1")
        adapter.activate()
        self.adapter = adapter

        metacbprx = adapter.addWithUUID(MetaCallback(self))
        self.metacb = Murmur.MetaCallbackPrx.uncheckedCast(metacbprx)

        authprx = adapter.addWithUUID(MumbleAuthenticator(self.app, self.logger, self.ice))
        self.auth = Murmur.ServerUpdatingAuthenticatorPrx.uncheckedCast(authprx)

        return self.attach_callbacks()

    def attach_callbacks(self):
        """
        Attaches all callbacks for meta and authenticators
        """

        try:
            self.logger.debug("Attaching meta callback")

            self.meta.addCallback(self.metacb)

            for server in self.meta.getBootedServers():
                self.logger.debug(
                    "Setting mumble authenticator for virtual server {}".format(server.id())
                )
                server.setAuthenticator(self.auth)
                self.attach_server_callback(server)

        except (
            Murmur.InvalidSecretException,
            Ice.UnknownUserException,
            Ice.ConnectionRefusedException,
        ) as e:
            if isinstance(e, Ice.ConnectionRefusedException):
                self.logger.warning("Server refused connection")
            elif (
                isinstance(e, Murmur.InvalidSecretException)
                or isinstance(e, Ice.UnknownUserException)
                and (e.unknown == "Murmur::InvalidSecretException")
            ):
                self.logger.error("Invalid ice secret")
            else:
                # We do not actually want to handle this one, re-raise it
                raise e

            self.connected = False
            return False

        self.connected = True
        return True

    def attach_server_callback(self, server):
        """Register DirectionEnforcementCallback for IN/OUT suppress enforcement.

        Guarded against duplicate registration — check_connection() calls
        attach_callbacks() every 10 seconds.  The guard is cleared by
        on_server_stopped() so a restarted virtual server gets a fresh
        callback correctly.
        """
        server_id = server.id()
        if server_id in self.server_callbacks:
            return

        cb = DirectionEnforcementCallback(self.app, self.logger, server)
        cbprx = self.adapter.addWithUUID(cb)
        server_cb = Murmur.ServerCallbackPrx.uncheckedCast(cbprx)

        try:
            server.addCallback(server_cb)
            self.server_callbacks[server_id] = server_cb
            self.logger.info(f"Direction enforcement callback attached to server {server_id}")
        except Exception as e:
            self.logger.error(f"Failed to attach server callback to {server_id}: {e}")

        self.sync_channels_from_groups(server)

    def sync_channels_from_groups(self, server):
        """Create a root-level Mumble channel for each OTS group lacking one.

        Channel name == group name so DirectionEnforcementCallback's lookup
        (which keys by channel name) keeps working.  Skips __ANON__.  Never
        deletes channels — too risky if users are mid-conversation; logs instead.
        """
        try:
            with self.app.app_context():
                from opentakserver.extensions import db
                from opentakserver.models.Group import Group
                rows = db.session.query(Group).all()
                group_names = {g.name for g in rows if g.name and g.name != "__ANON__"}

            if not group_names:
                return

            existing = server.getChannels()
            root_names = {ch.name for ch in existing.values() if ch.parent == 0}

            missing = group_names - root_names
            stale = root_names - group_names - {"Root"}

            for name in sorted(missing):
                try:
                    cid = server.addChannel(name, 0)
                    self.logger.info(
                        f"Mumble channel created for OTS group '{name}' "
                        f"(server={server.id()}, channel_id={cid})"
                    )
                except Exception as e:
                    self.logger.error(f"Failed to create channel '{name}': {e}")

            for name in sorted(stale):
                self.logger.warning(
                    f"Mumble channel '{name}' has no matching OTS group "
                    f"(server={server.id()}); leaving in place"
                )

            if self.app.config.get("OTS_MUMBLE_ENABLE_CONFERENCE_CALLS", True):
                self._ensure_temp_channel_acls(server, group_names)
        except Exception as e:
            self.logger.error(
                f"sync_channels_from_groups failed: {e}", exc_info=True
            )

    def _ensure_temp_channel_acls(self, server, managed_names):
        """Grant MakeTempChannel on Root and each OTS-managed channel.

        The OTS install's ACL model locks group channels to their members
        (revoke @all, grant only to <groupname>) and grants MakeTempChannel
        only to admins on Root.  That blocks the ATAK VX plugin's direct-call
        feature for non-admin users, since VX needs to create a temp channel
        for the 1:1.  This pass ORs MakeTempChannel into the auth/<groupname>/
        admin grants on the parents that gate creation.

        Channel-level ACLs on the temp itself are set separately by
        DirectionEnforcementCallback.channelCreated when a temp is created.
        """
        try:
            channels = server.getChannels()
        except Exception as e:
            self.logger.error(f"getChannels failed: {e}")
            return

        for channel_id, ch in channels.items():
            if channel_id != 0 and ch.name not in managed_names:
                continue
            self._ensure_make_temp_channel_acl(server, channel_id, ch.name)

    def _ensure_make_temp_channel_acl(self, server, channel_id, channel_name):
        """Idempotently set the ACL needed for VX direct calls on a persistent
        channel.  Three guarantees:

        1. The channel's own group/auth grant has MakeTempChannel so users
           can create temp channels under it (the call lifecycle).
        2. The `admin` grant has the full PERM_ADMIN_FULL mask with
           apply_sub=True so OTS administrators can administer the server
           (move, kick, ban, manage ACLs) on every OTS channel AND on any
           subchannel/temp created beneath it.
        3. An `auth` grant exists with apply_sub=True so temp channels
           created under this channel inherit Enter/Speak/Whisper/Text for
           any authenticated user.  On Root that grant also applies here
           (users can speak in Root itself).  On group channels apply_here
           stays False so the channel itself remains members-only.

        Guarantee #3 is what makes per-temp ACL setACL hooks unnecessary --
        the @auth grant flows down via inheritance so we don't have to
        intervene every time VX creates a temp.

        Only modifies non-inherited entries; inherited rows are filtered out
        before setACL (passing them back would shadow the parent's ACL).
        Idempotent — safe to re-run.
        """
        # Serialize get/modify/set so concurrent syncs (request_sync racing
        # with the startup attach path) can't TOCTOU-clobber each other or
        # overwrite a manual ACL edit that lands between the read and write.
        with self._acl_lock:
            try:
                acls, groups, inherit = server.getACL(channel_id)
            except Exception as e:
                self.logger.error(
                    f"getACL({channel_id}, name={channel_name}) failed: {e}"
                )
                return

            is_root = (channel_id == 0)
            own_acls = [a for a in acls if not a.inherited]

            targets = {"auth", "admin"}
            if channel_name and channel_name not in ("Root", "__ANON__"):
                targets.add(channel_name)

            dirty = False
            for acl in own_acls:
                if acl.group not in targets:
                    continue

                new_allow = acl.allow | PERM_MAKE_TEMP_CHANNEL
                new_apply_subs = acl.applySubs
                new_apply_here = acl.applyHere

                if acl.group == "admin":
                    new_allow |= PERM_ADMIN_FULL
                    new_apply_subs = True

                if acl.group == "auth":
                    # @auth must propagate to subchannels so VX temps inherit
                    # Enter+Speak+Whisper+TextMessage without per-temp intervention.
                    new_apply_subs = True
                    # On non-Root group channels, the channel itself MUST stay
                    # members-only (gated by the <groupname> ACL).  Force
                    # apply_here=False even if a prior buggy run or a manual
                    # edit left @auth applying to the channel itself, so the
                    # per-group privacy model can't drift open.
                    if not is_root:
                        new_apply_here = False

                if (new_allow != acl.allow
                        or new_apply_subs != acl.applySubs
                        or new_apply_here != acl.applyHere):
                    before_allow = acl.allow
                    before_sub = acl.applySubs
                    before_here = acl.applyHere
                    acl.allow = new_allow
                    acl.applySubs = new_apply_subs
                    acl.applyHere = new_apply_here
                    self.logger.info(
                        f"Updating ACL on channel_id={channel_id} name={channel_name} "
                        f"group={acl.group}: allow 0x{before_allow:x} -> 0x{acl.allow:x}, "
                        f"apply_here {before_here} -> {acl.applyHere}, "
                        f"apply_sub {before_sub} -> {acl.applySubs}"
                    )
                    dirty = True

            # Group channels (REACT, Family, etc.) typically have no own @auth
            # ACL -- access is gated entirely by the per-channel group grant.
            # Add an apply_here=False, apply_sub=True @auth grant so temps
            # under this channel inherit Enter+Speak for any authenticated
            # user.  The channel itself stays members-only.
            if not is_root and not any(a.group == "auth" for a in own_acls):
                own_acls.append(Murmur.ACL(
                    applyHere=False, applySubs=True, inherited=False,
                    userid=-1, group="auth", allow=PERM_BASELINE_SPEAK, deny=0,
                ))
                self.logger.info(
                    f"Adding @auth sub-grant on channel_id={channel_id} "
                    f"name={channel_name}: allow=0x{PERM_BASELINE_SPEAK:x}, apply_sub=True"
                )
                dirty = True

            if not dirty:
                return

            try:
                server.setACL(channel_id, own_acls, groups, inherit)
                self.logger.info(
                    f"Committed ACL update on channel_id={channel_id} name={channel_name}"
                )
            except Exception as e:
                self.logger.error(
                    f"setACL({channel_id}, name={channel_name}) failed: {e}",
                    exc_info=True,
                )

    def request_sync(self):
        """Trigger a channel sync on all booted servers off-thread.

        Called by group_api after add/delete so newly-created groups get a
        Mumble channel without waiting for the next service restart.
        """
        threading.Thread(target=self._sync_all_servers, daemon=True).start()

    def _sync_all_servers(self):
        try:
            for server in self.meta.getBootedServers():
                self.sync_channels_from_groups(server)
        except Ice.ConnectionRefusedException as e:
            # Murmur died between request_sync and now.  Mirror what
            # MetaCallback.stopped does -- flip connected=False so the
            # cleanup job and any other consumers of that flag stop using
            # the dead proxy.  The watchdog will repair state on its next
            # tick (~10s) when attach_callbacks reconnects.
            self.connected = False
            self.logger.warning(
                f"_sync_all_servers: Ice connection refused, marking disconnected: {e}"
            )
        except Exception as e:
            self.logger.error(f"_sync_all_servers: {e}", exc_info=True)

    def on_server_stopped(self, server_id):
        """Clear the callback guard and any cached session state for a stopped server.

        Without this, the duplicate-registration guard would prevent re-registration
        when the virtual server restarts.
        """
        self.server_callbacks.pop(server_id, None)
        self.logger.info(f"Cleared callback guard for stopped server {server_id}")

    def check_connection(self):
        """
        Tries reapplies all callbacks to make sure the authenticator
        survives server restarts and disconnects.
        """

        try:
            try:
                self.attach_callbacks()
            except Ice.Exception as e:
                self.logger.warning(
                    "{}: Failed connection check, will retry in next watchdog run ({}s)".format(e, 10)
                )
            except Exception as e:
                # Anything other than an Ice exception (a SQLAlchemy error
                # from sync_channels_from_groups, a runtime bug, etc.) would
                # otherwise propagate out and break the watchdog Timer chain,
                # leaving OTS unable to recover from future Murmur restarts.
                self.logger.error(
                    f"Unexpected error in watchdog reattach: {e}", exc_info=True
                )
        finally:
            # Always re-arm the timer, even if attach_callbacks raised
            # something unexpected.  daemon=True so the timer thread doesn't
            # block process shutdown.
            self.watchdog = Timer(10, self.check_connection)
            self.watchdog.daemon = True
            self.watchdog.start()


class DirectionEnforcementCallback(Murmur.ServerCallback):
    """Enforces OTS IN/OUT speak direction by setting Murmur's suppress flag.

    Channel access (who can enter which channel) is controlled by Murmur's
    own ACL configuration.  This callback's only job is to mute users whose
    OTS group membership has direction=OUT (listen-only) and unmute those
    with direction=IN.

    All Ice proxy calls (getState/setState) are dispatched to a background
    daemon thread to avoid deadlocking the Ice thread pool.
    """

    def __init__(self, app, logger, server):
        Murmur.ServerCallback.__init__(self)
        self.app = app
        self.logger = logger
        self.server = server
        self.server_id = server.id()
        self._channel_cache = None
        self._channel_cache_time = 0
        self._session_lock = threading.Lock()
        self._session_cache = {}  # session_id -> {directions, is_admin, cached_at}
        # Sessions we've actively set suppress=True for.  Used so steady-state
        # channel moves make zero Ice calls -- we only round-trip to Murmur
        # when a user actually crosses an IN<->OUT boundary.  For environments
        # with no OUT memberships this set stays empty forever and direction
        # enforcement is effectively free.
        self._suppressed_sessions = set()
        self._suppress_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _get_channel_map(self):
        """Return {channel_id: channel_name}, cached for 60 seconds."""
        if self._channel_cache is None or (time.time() - self._channel_cache_time) > 60:
            try:
                channels = self.server.getChannels()
                self._channel_cache = {cid: ch.name for cid, ch in channels.items()}
                self._channel_cache_time = time.time()
            except Exception as e:
                self.logger.error(f"Failed to refresh channel map: {e}")
                return self._channel_cache or {}
        return self._channel_cache

    def _get_user_directions(self, session_id, username):
        """Return (group_directions dict, is_admin) for a user, cached for 30 seconds.

        group_directions maps group_name -> 'IN' or 'OUT'.
        Prefers IN over OUT when a user has both rows for the same group.
        """
        cache_ttl = 30
        now = time.time()

        with self._session_lock:
            cached = self._session_cache.get(session_id)
            if cached and (now - cached['cached_at']) < cache_ttl:
                return cached['directions'], cached['is_admin']

        group_directions = {}
        is_admin = False

        try:
            with self.app.app_context():
                # Reuse the authenticator's lookup chain (username -> callsign ->
                # base callsign -> underscore->space) so direction enforcement
                # finds users by the same path Mumble auth used.
                user, _ = MumbleAuthenticator.resolve_identity(self.app, username)

                if not user:
                    self.logger.warning(f"Direction lookup: OTS user not found for '{username}'")
                    return {}, False

                for membership in user.group_memberships:
                    if not membership.enabled:
                        continue
                    grp = membership.group.name
                    # Prefer IN over OUT if both rows exist for the same group
                    if group_directions.get(grp) != 'IN':
                        group_directions[grp] = membership.direction

                is_admin = any(r.name == 'administrator' for r in user.roles)
        except Exception as e:
            self.logger.error(f"Direction lookup failed for '{username}': {e}", exc_info=True)
            return {}, False

        with self._session_lock:
            self._session_cache[session_id] = {
                'directions': group_directions,
                'is_admin': is_admin,
                'cached_at': now,
            }

        return group_directions, is_admin

    def _dispatch_apply(self, session, username, channel_id, group_directions, is_admin):
        """Dispatch only the Ice state calls to a background thread.

        DB queries run in the Ice dispatch thread (same as authenticate() — works fine).
        Only getState()/setState() must be off-thread to avoid deadlocking the Ice pool.
        """
        threading.Thread(
            target=self._apply_direction,
            args=(session, username, channel_id, group_directions, is_admin),
            daemon=True,
        ).start()

    def _apply_direction(self, session, username, channel_id, group_directions, is_admin):
        """Apply the suppress flag based on the user's OTS direction.

        Uses in-memory tracking (_suppressed_sessions) so the common case --
        a user moving between channels where direction is IN or undefined --
        makes zero Ice calls.  Only round-trips to Murmur when the desired
        suppress bit actually has to flip.
        """
        try:
            if is_admin:
                return

            channel_map = self._get_channel_map()
            channel_name = channel_map.get(channel_id, f"unknown({channel_id})")

            # Root and non-OTS channels (VX temps, event channels) are not
            # subject to direction enforcement -- treat as if direction=IN.
            direction = group_directions.get(channel_name)
            should_suppress = False  # IN/OUT both grant speak; direction-based mute disabled per rally-day policy

            with self._suppress_lock:
                currently_suppressed = session in self._suppressed_sessions

            if should_suppress == currently_suppressed:
                return  # No state change needed; no Ice call.

            try:
                s = self.server.getState(session)
            except Murmur.InvalidSessionException:
                # Session disconnected between dispatch and execution -- normal.
                self.logger.debug(
                    f"Direction: session {session} ({username}) gone before getState"
                )
                with self._suppress_lock:
                    self._suppressed_sessions.discard(session)
                return

            s.suppress = should_suppress
            try:
                self.server.setState(s)
            except Murmur.InvalidSessionException:
                self.logger.debug(
                    f"Direction: session {session} ({username}) gone before setState"
                )
                with self._suppress_lock:
                    self._suppressed_sessions.discard(session)
                return

            with self._suppress_lock:
                if should_suppress:
                    self._suppressed_sessions.add(session)
                else:
                    self._suppressed_sessions.discard(session)

            if should_suppress:
                self.logger.info(
                    f"LISTEN ONLY: {username} in {channel_name} (direction=OUT)"
                )
                try:
                    self.server.sendMessage(
                        session,
                        f"<b>Listen Only:</b> You are receive-only in {channel_name}.",
                    )
                except Exception:
                    pass
            else:
                self.logger.info(
                    f"SPEAK ENABLED: {username} in {channel_name} "
                    f"(direction={direction or 'non-OTS'})"
                )

        except Murmur.InvalidSessionException:
            self.logger.debug(
                f"Direction: session {session} ({username}) gone during apply"
            )
            with self._suppress_lock:
                self._suppressed_sessions.discard(session)
        except Exception as e:
            self.logger.error(
                f"Unhandled error applying direction for '{username}' session={session}: {e}",
                exc_info=True,
            )

    # ----------------------------------------------------------- Ice callbacks

    def _record_activity(self, channel_id):
        ice_app = self.app.extensions.get("mumble_ice_app")
        if ice_app is not None:
            ice_app.record_channel_activity(channel_id)

    def userConnected(self, state, current=None):
        self.logger.info(
            f"User connected: {state.name} (session={state.session}, userid={state.userid}) "
            f"channel={state.channel}"
        )
        self._record_activity(state.channel)
        # DB lookup runs here in the Ice dispatch thread (safe — same as authenticate())
        directions, is_admin = self._get_user_directions(state.session, state.name)
        # Only the Ice getState/setState calls go to a background thread
        self._dispatch_apply(state.session, state.name, state.channel, directions, is_admin)

    def userDisconnected(self, state, current=None):
        with self._session_lock:
            self._session_cache.pop(state.session, None)
        with self._suppress_lock:
            self._suppressed_sessions.discard(state.session)
        self.logger.info(f"User disconnected: {state.name} (session={state.session})")

    def userStateChanged(self, state, current=None):
        """Fire on any state change — channel moves trigger direction re-check."""
        self._record_activity(state.channel)
        directions, is_admin = self._get_user_directions(state.session, state.name)
        self._dispatch_apply(state.session, state.name, state.channel, directions, is_admin)

    def userTextMessage(self, state, message, current=None):
        pass

    def channelCreated(self, state, current=None):
        # Per-temp ACL intervention is no longer needed: the @auth grant on
        # parent channels (set by _ensure_make_temp_channel_acl) propagates
        # to temps via inheritance, and Murmur auto-creates a creator-admin
        # group on each temp for VX's call lifecycle management.  We only
        # need to invalidate the channel cache here.
        self._channel_cache = None

    def channelRemoved(self, state, current=None):
        self._channel_cache = None

    def channelStateChanged(self, state, current=None):
        self._channel_cache = None


class MetaCallback(Murmur.MetaCallback):
    def __init__(self, authenticator):
        Murmur.MetaCallback.__init__(self)
        self.authenticator = authenticator

    def started(self, server, current=None):
        """
        This function is called when a virtual server is started
        and makes sure an authenticator gets attached if needed.
        """
        server_id = server.id()
        self.authenticator.logger.info(
            "Virtual server {} started — attaching authenticator and direction callback".format(server_id)
        )
        try:
            server.setAuthenticator(self.authenticator.auth)
            self.authenticator.attach_server_callback(server)
        # Apparently this server was restarted without us noticing
        except (Murmur.InvalidSecretException, Ice.UnknownUserException) as e:
            if hasattr(e, "unknown") and e.unknown != "Murmur::InvalidSecretException":
                # Special handling for Murmur 1.2.2 servers with invalid slice files
                raise e

            return

    def stopped(self, server, current=None):
        """
        This function is called when a virtual server is stopped
        """
        if self.authenticator.connected:
            # Only try to output the server id if we think we are still connected to prevent
            # flooding of our thread pool
            try:
                server_id = server.id()
                self.authenticator.logger.info(
                    "Virtual server {} stopped — clearing callback guard".format(server_id)
                )
                self.authenticator.on_server_stopped(server_id)
                return
            except Ice.ConnectionRefusedException:
                self.authenticator.connected = False

        self.authenticator.logger.info("Server shutdown stopped a virtual server")
