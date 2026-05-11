# Server-side support for the ATAK VX voice plugin

This doc covers how OpenTAKServer integrates with Mumble (Murmur) to support
the [ATAK VX](https://www.civtak.org/wiki/index.php/ATAK_VX_Voice) voice
plugin, including direct calls and conference calls between users in
different OTS groups.

## Architecture at a glance

```
ATAK device  ──TLS──>  Murmur (mumble-server)
                         │
                         │ Ice (TCP 6502, secret-gated)
                         ▼
                       OpenTAKServer
                       ├── MumbleAuthenticator   (authenticates clients)
                       ├── MumbleIceApp          (channel sync + ACL helpers)
                       └── DirectionEnforcement  (suppress/unmute by OTS group)
```

Every Mumble client that connects to Murmur is authenticated by OTS via the
Murmur Ice authenticator interface, not by Murmur's local user table. OTS
returns the user's Mumble user id, display name, and the list of Mumble
groups they belong to (one per OTS group, plus `admin` for OTS administrators).

The Ice connection is initialized by `MumbleIceDaemon` (background thread)
and handed off to `MumbleIceApp`, which:

1. Registers the authenticator with every booted virtual server.
2. Registers a per-server `DirectionEnforcementCallback` that watches user
   state changes for IN/OUT direction enforcement *and* records channel
   activity for the cleanup job.
3. Auto-creates a Mumble channel for every OTS group on startup and on
   demand from the group_api (see `sync_channels_from_groups`).

## How VX uses the integration

The official ATAK VX voice plugin opens **two parallel TLS connections** per
device — each authenticates to Murmur with the user's callsign suffixed with
a unique UUID (`<callsign>---<uuid>`). OTS resolves both UUIDs back to the
same OTS user via:

1. Exact callsign match.
2. `---<uuid>` suffix stripped → callsign match.
3. Underscore → space conversion (`EUD_Charlie_MXVX` → `EUD Charlie MXVX`).
4. Cert CN (the EUD UID) → EUD lookup. This survives mid-session callsign
   renames where the callsign change hasn't propagated through CoT yet.

Each socket gets its own Mumble user id (base id + deterministic
hash-of-callsign offset, modulo the per-user 1000-id range), so both sockets
co-exist without colliding even though they belong to the same OTS user.

### Direct (1:1) calls

When the user initiates a direct call from VX:

1. VX creates a **temporary Mumble channel** under either Root or the
   user's current channel. This requires the `MakeTempChannel` (`0x400`)
   permission on the parent channel.
2. The receiving side's VX joins the temp channel by name/id (self-join via
   coordination over OTS CoT messages — VX clients do not drag each other
   between channels).
3. Both clients speak in the temp. When both leave, Murmur garbage-collects
   the temp automatically.

### Conference calls

Conferences are a generalization of direct calls — the temp channel can hold
N participants, including users from different OTS groups. The ACL set on
the temp at creation time (see below) ensures any authenticated user can
enter once invited, regardless of which OTS group they belong to.

## ACL model

OTS deliberately operates a **locked-down per-channel ACL model**: each
auto-synced channel revokes baseline access from `@all` and grants it back
only to the channel's named group, so group channels are private to their
members.

| Channel | Group | Grant mask | apply_here / apply_sub | Notes |
|---|---|---|---|---|
| Root (id=0) | `@all` | `Traverse` (`0x2`) | here=1, sub=1 | Visible to everyone, propagates to subchannels |
| Root | `auth` | `0x70e` | here=1, sub=1 | Lets any auth user speak in Root AND inherits Enter+Speak to temps |
| Root | `admin` | `0x707ff` | here=1, sub=1 | Full Murmur admin, inherits to all subchannels |
| Group channels (one per OTS group) | `all` | revoke `0x30e` | here=1, sub=0 | Strip baseline access from non-members; channel itself only |
| Group channels | `<groupname>` | `0x70e` | here=1, sub=0 | Members get speak + MakeTempChannel for in-group temps |
| Group channels | `admin` | `0x707ff` | here=1, sub=1 | Full admin on the channel AND inherits to any temp beneath |
| Group channels | `auth` | `0x30e` | here=0, sub=1 | **Sub-only grant** — temps inherit Enter/Speak/Whisper/Text without making the group channel itself public |
| Temp channels (created by VX) | (mostly inherited) | — | — | See "How temp ACLs work" below — we don't override |

**Move (`0x20`) is intentionally not granted to non-admin groups.** Standard
users cannot drag other users between channels — only admins can. VX direct
calls work via self-join (and via the caller's own creator-admin power on
their own temp), not by the initiator dragging the recipient.

### How temp channel ACLs work (inheritance, not per-temp setACL)

We do **not** call `setACL` on every newly created temp. Murmur's own
behavior handles per-temp permissions correctly as long as the parent
channels are set up right:

1. **Murmur auto-creates a local `admin` group** on each new temp and
   adds the **creator's userid** to it. This gives the creator full
   Write/Move/etc. on their own temp via the inherited `admin/0x707ff`
   grant from Root — even if the creator is a non-admin OTS user. That's
   how VX's "Updated ACL in channel" call (which the plugin issues
   ~160ms after channel creation) succeeds for everyone.

2. **The `@auth` apply_sub=1 grant on parent channels** propagates
   Enter+Speak+Whisper+TextMessage down to the temp. On Root this means
   any auth user can enter a Root-level temp; on group channels it means
   any auth user (including someone from a different OTS group) can
   enter the temp, enabling cross-group conferences.

3. **`@all` apply_sub=1 from Root** propagates `Traverse` so the temp is
   visible to everyone.

Net effect: zero Ice calls during the VX call lifecycle. The temp's
permissions are correct by inheritance from its parent.

### How the persistent ACLs are kept correct

`MumbleIceApp.sync_channels_from_groups` runs on startup and on demand
(from `group_api.request_sync()` after add/delete). It walks Root + every
OTS-managed channel and calls `_ensure_make_temp_channel_acl`, which
idempotently ensures each channel has:

1. `MakeTempChannel` (`0x400`) on the `<groupname>` / `auth` / `admin` grants
   so users can create call channels.
2. `admin` set to the full `0x707ff` grant with `apply_sub=True` so admins
   keep server-administrator perms on every OTS channel and any subchannel.
3. An `@auth` grant of `0x30e` with `apply_here=False, apply_sub=True` on
   group channels (added if missing). This is the sub-only grant that
   makes temps inherit Enter+Speak.

The function reads existing ACLs, modifies in place, filters out inherited
rows, and writes back. Idempotent — runs that find nothing to change are
silent. Manual ACL edits through Mumble's GUI are preserved (we only
ensure the specific bits/grants above; other entries are untouched).

## Channel cleanup

The auto-sync only *creates* channels; it never deletes them. To prevent
stale event channels from accumulating, `cleanup_unmanaged_mumble_channels`
runs every 2 days (configurable via the `JOBS` entry in `defaultconfig.py`)
and deletes root-level Mumble channels that:

- Are **not** the Root channel
- Are **not** named after an existing OTS group
- Are **not** Murmur temp channels (Murmur GCs those itself)
- Have **no users** currently in them
- Have not had any user activity for `OTS_MUMBLE_CHANNEL_CLEANUP_IDLE_DAYS`
  days (default 5)

Activity is recorded in-memory by `DirectionEnforcementCallback` on every
`userConnected` and `userStateChanged`. After a service restart, channels
with no recorded activity fall back to the service start time — so an
event channel needs a full idle window of running service before it
qualifies for deletion. This avoids accidentally deleting a long-empty
channel right after a restart, at the cost of delaying cleanup if the
service restarts often.

To create an event channel that should survive: just create it in the
Mumble GUI and use it. As long as someone joins it within the idle window
(or someone is in it at the moment the cleanup job runs), it will not be
deleted.

## Configuration

| Key | Default | What it does |
|---|---|---|
| `OTS_ENABLE_MUMBLE_AUTHENTICATION` | `False` | Master switch — enables the Ice authenticator and the daemon |
| `OTS_ICE_SECRET` | `""` | Must match Murmur's `icesecretread`/`icesecretwrite` in `/etc/mumble-server.ini` |
| `OTS_MUMBLE_ENABLE_CONFERENCE_CALLS` | `True` | Adds the ACL grants that enable non-admin VX calls: MakeTempChannel on Root + each OTS-managed channel, @auth `apply_sub=True` sub-grant on group channels (temps inherit Enter/Speak), and full admin grant on every OTS channel. Set `False` to revert to the legacy admin-only model |
| `OTS_MUMBLE_CHANNEL_CLEANUP_ENABLED` | `True` | Master switch for the periodic cleanup job |
| `OTS_MUMBLE_CHANNEL_CLEANUP_IDLE_DAYS` | `5` | How long a channel must be idle before deletion |

The cleanup interval (every 2 days) lives in the `JOBS` list in
`defaultconfig.py` rather than as a separate config key, since it follows
the same pattern as every other scheduled OTS job.

## Troubleshooting

### Standard user gets "Permission denied" when creating a temp channel

Confirm `OTS_MUMBLE_ENABLE_CONFERENCE_CALLS` is `True` in your config and
OTS has been restarted at least once after upgrading. On startup you
should see lines like:

```
Updating ACL on channel_id=0 name=Root group=auth: allow 0x70e -> 0x70e, apply_sub False -> True
Adding @auth sub-grant on channel_id=6 name=REACT: allow=0x30e, apply_sub=True
Committed ACL update on channel_id=6 name=REACT
```

If those lines never appear on startup, OTS's Ice connection to Murmur is
failing — check `OTS_ICE_SECRET` matches what's in `/etc/mumble-server.ini`.

Once the ACLs are in place they're idempotent — subsequent startups won't
re-log them. To verify the live ACL on the Murmur side directly:

```sh
sudo sqlite3 -header -column /var/lib/mumble-server/mumble-server.sqlite \
  "SELECT channel_id, priority, group_name, apply_here, apply_sub, \
          printf('0x%x', grantpriv) AS grant_hex, \
          printf('0x%x', revokepriv) AS revoke_hex \
   FROM acl ORDER BY channel_id, priority;"
```

Expected on each group channel: an `auth` row with `apply_here=0, apply_sub=1,
grant_hex=0x30e`, plus `admin` row with `apply_sub=1, grant_hex=0x707ff`, plus
`<groupname>` row with `grant_hex=0x70e`.

### User from group A can't join a temp channel created by group B

Inheritance might not be propagating. Check the live ACL (sqlite query
above) on the parent of the temp channel. The parent (Root or whichever
OTS group channel) must have a `@auth` row with `apply_sub=1` and
`grant_hex` of at least `0x30e`. If that row is missing, run an OTS
restart — `_ensure_make_temp_channel_acl` will add it on startup.

VX clients also need to coordinate the call (the signaling layer is
plugin-internal and not visible to the server). If the ACL is correct
but the callee still can't reach the temp, check VX-side first
(server-side ACL is no longer the bottleneck).

### Standard user can't drag another user into a temp

This is **expected behavior**. `Move` is admin-only by design at the OTS
group level. The caller doesn't need it for VX — VX moves users via
whisper (VoiceTarget) and via each client's own self-join, not by the
initiator dragging the recipient. The caller also has `Move` *on their
own temp* automatically via Murmur's creator-admin group, which is enough
for any in-call moves the plugin needs to do.

### Stale event channels accumulating

The cleanup job runs every 2 days. If you've just deployed and want to
clear stale channels immediately, you can either:

1. Wait for the next scheduled run (visible in the OTS scheduler API).
2. Trigger it manually via the scheduler API:
   ```
   POST /api/scheduler/jobs/cleanup_unmanaged_mumble_channels/run
   ```
   (auth as an OTS administrator)

If a channel is *not* getting deleted that you think should be, check:
- Is its name in the OTS Group table? Channels matching an OTS group are
  always preserved.
- Is anyone currently in the channel?
- Was the channel just created? After a service restart it needs a full
  `OTS_MUMBLE_CHANNEL_CLEANUP_IDLE_DAYS` window of running service before
  it's eligible.

## Implementation references

| Concern | File / function |
|---|---|
| Authenticator (resolves callsign-with-UUID, cert CN, etc.) | `opentakserver/mumble/mumble_authenticator.py::MumbleAuthenticator.resolve_identity` |
| Persistent-channel ACL setup (MakeTempChannel + admin elevation + @auth sub-grant) | `opentakserver/mumble/mumble_ice_app.py::MumbleIceApp._ensure_make_temp_channel_acl` |
| `channelCreated` callback (now just cache invalidation — no per-temp setACL) | `opentakserver/mumble/mumble_ice_app.py::DirectionEnforcementCallback.channelCreated` |
| Direction enforcement (in-memory suppress tracking) | `opentakserver/mumble/mumble_ice_app.py::DirectionEnforcementCallback._apply_direction` |
| Channel auto-sync from OTS groups | `opentakserver/mumble/mumble_ice_app.py::MumbleIceApp.sync_channels_from_groups` |
| Idle channel cleanup job | `opentakserver/blueprints/scheduled_jobs.py::cleanup_unmanaged_mumble_channels` |
| Murmur permission constants | `opentakserver/mumble/Murmur.ice` (search `Permission*`) |
| VX direct-call protocol (sister doc) | [`vx-direct-call-protocol.md`](./vx-direct-call-protocol.md) |

## Operational caveats

- **OTS auto-recovers after a Murmur restart.** A 10-second watchdog timer
  in `MumbleIceApp.check_connection()` reattaches the meta callback and
  authenticator after a Murmur restart. There's still a brief window
  (up to 10s) where new connections are rejected with
  `Wrong certificate or password for existing user` until the watchdog
  reattaches. If you need a faster recovery, restart OTS too — but you
  no longer *have* to.

- **VX 1.0.0 clients reconnect every few minutes by design.** You'll see
  bursts of simultaneous `Connection closed` + `New connection` lines for
  every VX client at roughly 3-5 minute intervals, even when nothing is
  happening. This is the plugin's TLS reset cycle. Not a bug.

- **Caller appears stuck in Root after End Call for up to ~60 seconds.**
  After ending a call, VX moves the caller to Root as an intermediate
  state, then to their original channel. The delay between the two moves
  is client-side and varies by VX version. Nothing OTS can do about it
  without a server-side auto-move-on-auth feature (which would mask the
  symptom but introduce its own preference logic).
