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
        props.setProperty("Ice.Default.InvocationTimeout", str(30 * 1000))
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
        # Expose this daemon to Flask blueprints so group_api can request channel
        # syncs after add/delete.  app.extensions is a plain dict; reads are thread-safe.
        self.app.extensions["mumble_ice_app"] = self

    def record_channel_activity(self, channel_id):
        """Stamp `now` as the last-active time for a channel.  Called by the
        direction-enforcement callback on connect / channel-hop, and by the
        cleanup job when it observes a channel that's currently occupied."""
        with self._channel_activity_lock:
            self._channel_last_active[channel_id] = datetime.now(timezone.utc)

    def run(self, *args):
        if not self.initialize_ice_connection():
            self.logger.error("Mumble server connection failed")
            return 1

        self.check_connection()

        self.watchdog.cancel()

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
        """Idempotently OR MakeTempChannel into auth/<channel_name>/admin grants.

        Only modifies non-inherited entries; inherited rows are filtered out
        before setACL (passing them back would shadow the parent's ACL and
        break propagation).  Skips any existing entry that already has the
        bit set, so this is safe to re-run on every watchdog tick.
        """
        try:
            acls, groups, inherit = server.getACL(channel_id)
        except Exception as e:
            self.logger.error(
                f"getACL({channel_id}, name={channel_name}) failed: {e}"
            )
            return

        targets = {"auth", "admin"}
        if channel_name and channel_name not in ("Root", "__ANON__"):
            targets.add(channel_name)

        own_acls = [a for a in acls if not a.inherited]
        dirty = False
        for acl in own_acls:
            if acl.group not in targets:
                continue

            new_allow = acl.allow | PERM_MAKE_TEMP_CHANNEL
            new_apply_subs = acl.applySubs

            # OTS administrators get the same full grant Murmur uses on Root
            # (Write, Move, Kick, Ban, Register, MakeTempChannel, etc.) with
            # apply_sub=1 so the grant propagates into any sub/temp channel
            # created beneath an OTS-managed channel.  This is what makes
            # "OTS admin" mean "Mumble server administrator" — they can move
            # users, kick, ban, manage ACLs, etc. on every OTS channel.
            if acl.group == "admin":
                new_allow |= PERM_ADMIN_FULL
                new_apply_subs = True

            if new_allow != acl.allow or new_apply_subs != acl.applySubs:
                before_allow = acl.allow
                before_sub = acl.applySubs
                acl.allow = new_allow
                acl.applySubs = new_apply_subs
                self.logger.info(
                    f"Updating ACL on channel_id={channel_id} name={channel_name} "
                    f"group={acl.group}: allow 0x{before_allow:x} -> 0x{acl.allow:x}, "
                    f"apply_sub {before_sub} -> {acl.applySubs}"
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
            self.attach_callbacks()
        except Ice.Exception as e:
            self.logger.warning(
                "{}: Failed connection check, will retry in next watchdog run ({}s)".format(e, 10)
            )

        # Renew the timer
        self.watchdog = Timer(10, self.check_connection)
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
        """Background thread: apply suppress flag via Ice calls only."""
        try:
            # Admins are never suppressed by this callback, so skip all Ice calls.
            # Calling getState() here blocks for 30s and crashes the connection.
            if is_admin:
                return

            channel_map = self._get_channel_map()
            channel_name = channel_map.get(channel_id, f"unknown({channel_id})")

            # Root channel: always allow speaking
            if channel_name == 'Root':
                try:
                    s = self.server.getState(session)
                    if s.suppress:
                        s.suppress = False
                        self.server.setState(s)
                        self.logger.info(f"UNMUTED (Root): {username}")
                except Exception as e:
                    self.logger.error(f"Failed to clear suppress for '{username}' in Root: {e}")
                return

            direction = group_directions.get(channel_name)
            if direction is None:
                # Non-OTS channel (temp channel for VX private call, or an
                # admin-created event channel).  Direction enforcement is an
                # OTS group concept; outside an OTS group, a user who was
                # suppressed in their previous channel must be un-suppressed
                # so they can actually speak in the call they just joined.
                try:
                    s = self.server.getState(session)
                    if s.suppress:
                        s.suppress = False
                        self.server.setState(s)
                        self.logger.info(
                            f"SPEAK ENABLED (non-OTS channel): {username} in {channel_name}"
                        )
                except Exception as e:
                    self.logger.error(
                        f"Failed to clear suppress for '{username}' in {channel_name}: {e}"
                    )
                return

            s = self.server.getState(session)
            should_suppress = (direction == 'OUT')

            if should_suppress and not s.suppress:
                s.suppress = True
                self.server.setState(s)
                self.logger.info(f"LISTEN ONLY: {username} in {channel_name} (direction=OUT)")
                try:
                    self.server.sendMessage(
                        session,
                        f"<b>Listen Only:</b> You are receive-only in {channel_name}.",
                    )
                except Exception:
                    pass

            elif not should_suppress and s.suppress:
                s.suppress = False
                self.server.setState(s)
                self.logger.info(f"SPEAK ENABLED: {username} in {channel_name} (direction=IN)")

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
        self.logger.info(f"User disconnected: {state.name} (session={state.session})")

    def userStateChanged(self, state, current=None):
        """Fire on any state change — channel moves trigger direction re-check."""
        self._record_activity(state.channel)
        directions, is_admin = self._get_user_directions(state.session, state.name)
        self._dispatch_apply(state.session, state.name, state.channel, directions, is_admin)

    def userTextMessage(self, state, message, current=None):
        pass

    def channelCreated(self, state, current=None):
        self._channel_cache = None
        if not state.temporary:
            return
        if not self.app.config.get("OTS_MUMBLE_ENABLE_CONFERENCE_CALLS", True):
            return
        # Dispatch the ACL set off the Ice thread — setACL is a synchronous
        # Ice call and would otherwise block the dispatcher.  Same pattern as
        # _dispatch_apply for direction enforcement.
        threading.Thread(
            target=self._apply_temp_channel_acl,
            args=(state.id, state.name),
            daemon=True,
        ).start()

    def _apply_temp_channel_acl(self, channel_id, channel_name):
        """Augment a freshly-created temp channel with a conference-capable ACL.

        Murmur's default temp setup already grants the creator admin perms on
        their own temp (via an auto-created local `admin` group with the
        creator's userid added) plus inherits @all/Traverse and admin/full from
        Root.  That's what lets the VX plugin manage its temp post-creation
        (set call ACL, move users at end-call, delete the temp).  But the
        default doesn't grant non-creator @auth users Enter/Speak — so a
        callee from a different OTS group can't join.

        This function ADDS an explicit @auth=Enter|Speak|Whisper|TextMessage
        entry while preserving Murmur's defaults (inherit=True, all local
        groups kept).  Result: cross-group conferences work AND creator
        retains admin on their own temp for VX's call lifecycle.
        """
        try:
            # Read what Murmur set up: implicit creator-admin group + inherited
            # ACLs.  We preserve everything and just layer @auth Enter/Speak on top.
            try:
                pre_acls, pre_groups, _pre_inherit = self.server.getACL(channel_id)
            except Exception as e:
                self.logger.error(
                    f"getACL on new temp id={channel_id} failed, skipping ACL set: {e}"
                )
                return

            # Keep only locally-defined groups (notably the creator-admin entry
            # Murmur auto-creates).  Inherited groups are recreated by Murmur
            # from the parent chain — passing them back would shadow inheritance.
            local_groups = [g for g in pre_groups if not g.inherited]

            # Keep existing non-inherited ACLs (in case Murmur or VX set
            # something specific), then add our @auth Enter/Speak grant.
            own_acls = [a for a in pre_acls if not a.inherited]
            has_auth_speak = any(
                a.group == "auth" and (a.allow & PERM_BASELINE_SPEAK) == PERM_BASELINE_SPEAK
                for a in own_acls
            )
            if not has_auth_speak:
                own_acls.append(Murmur.ACL(
                    applyHere=True, applySubs=False, inherited=False,
                    userid=-1, group="auth", allow=PERM_BASELINE_SPEAK, deny=0,
                ))

            # inherit=True preserves @all/Traverse + admin/full from Root, plus
            # the creator-admin group membership chain.
            self.server.setACL(channel_id, own_acls, local_groups, True)

            preserved_creator = next(
                (g for g in local_groups if g.name == "admin" and g.add), None
            )
            creator_uids = list(preserved_creator.add) if preserved_creator else []
            self.logger.info(
                f"Temp channel ACL augmented: id={channel_id} name='{channel_name}' "
                f"added auth=0x{PERM_BASELINE_SPEAK:x}, preserved creator-admin uids={creator_uids}"
            )
        except Exception as e:
            self.logger.error(
                f"Failed to set temp channel ACL on id={channel_id} "
                f"name='{channel_name}': {e}",
                exc_info=True,
            )

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
