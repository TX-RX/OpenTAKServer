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

| Channel | Group | Grant mask | Notes |
|---|---|---|---|
| Root (id=0) | `@all` | `Traverse` | Visible to everyone |
| Root | `auth` | `Traverse \| Enter \| Speak \| Whisper \| TextMessage \| MakeTempChannel` (`0x70e`) | Lets any authenticated user open a VX temp at root level |
| Root | `admin` | All admin perms (`0x707ff`) | Full Murmur admin |
| Group channels (one per OTS group) | `all` | revoke `0x30e` | Strip baseline access from non-members |
| Group channels | `<groupname>` | `0x70e` | Members get speak + MakeTempChannel for in-group temps |
| Group channels | `admin` | `0x77f` | Admins keep full per-channel control incl. temp creation |
| Temp channels (created by VX) | `@all` | `Traverse` | Discoverable by all |
| Temp channels | `auth` | `0x30e` | Any authenticated user can enter and speak — this is what makes cross-group conferences work |
| Temp channels | `admin` | `0x707ff` | Admins keep full control |

**Move (`0x20`) is intentionally not granted to non-admin groups.** Standard
users cannot drag other users between channels — only admins can. VX direct
calls work via self-join, not by the initiator dragging the recipient.

### How the temp ACL is applied

`DirectionEnforcementCallback.channelCreated` fires every time a channel is
created. When `state.temporary == True`, the callback dispatches an Ice
`setACL(channel_id, [...], inherit=False)` on a background thread. The
ACL is fully self-contained (`inherit=False`) so the temp's behavior is
identical regardless of where VX places it — whether under Root or under a
locked-down group channel that wouldn't propagate any useful ACL via
inheritance.

### How the persistent ACLs are kept correct

`MumbleIceApp.sync_channels_from_groups` runs on startup and on every
watchdog tick (10s). It walks Root + every OTS-managed channel and calls
`_ensure_make_temp_channel_acl`, which:

1. Reads the channel's ACL via `getACL`.
2. Filters out inherited entries (passing them back would shadow parents).
3. ORs `MakeTempChannel` (`0x400`) into the `auth` / `<channel-name>` /
   `admin` grants if missing.
4. Writes back via `setACL`. Idempotent — runs that don't change anything
   are silent.

This pattern survives manual ACL edits through Mumble's GUI: only the
`MakeTempChannel` bit is forced, every other grant the admin set up by hand
is preserved.

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
| `OTS_MUMBLE_ENABLE_CONFERENCE_CALLS` | `True` | Grants MakeTempChannel to non-admins and applies the conference ACL to new temp channels. Set `False` to revert to the legacy admin-only model |
| `OTS_MUMBLE_CHANNEL_CLEANUP_ENABLED` | `True` | Master switch for the periodic cleanup job |
| `OTS_MUMBLE_CHANNEL_CLEANUP_IDLE_DAYS` | `5` | How long a channel must be idle before deletion |

The cleanup interval (every 2 days) lives in the `JOBS` list in
`defaultconfig.py` rather than as a separate config key, since it follows
the same pattern as every other scheduled OTS job.

## Troubleshooting

### Standard user gets "Permission denied" when creating a temp channel

Confirm `OTS_MUMBLE_ENABLE_CONFERENCE_CALLS` is `True` in your config and
the OTS service has been restarted at least once after upgrading. On
startup you should see lines like:

```
Granting MakeTempChannel on channel_id=N name=<name> group=<group>: 0x30e -> 0x70e
Committed ACL update on channel_id=N name=<name>
```

If those lines never appear, OTS's Ice connection to Murmur is failing —
check `OTS_ICE_SECRET` matches what's in `/etc/mumble-server.ini`.

To inspect the live ACL on the Murmur side directly:

```sh
sudo sqlite3 -header -column /var/lib/mumble-server/mumble-server.sqlite \
  "SELECT channel_id, priority, group_name, \
          printf('0x%x', grantpriv) AS grant_hex, \
          printf('0x%x', revokepriv) AS revoke_hex \
   FROM acl ORDER BY channel_id, priority;"
```

The `auth` row on Root and the `<groupname>` rows on each managed channel
should have `0x70e` (or higher) in `grant_hex`.

### User from group A can't join a temp channel created by group B

Confirm the temp channel actually got the conference ACL applied — the OTS
log should contain a line like:

```
Temp channel ACL set: id=N name='<temp-name>' (auth=0x30e, admin=0x707ff)
```

If it's missing, the `channelCreated` callback may not be firing — check
that `Direction enforcement callback attached to server N` appeared on
startup, and that there are no exceptions from `_apply_temp_channel_acl`.

### Standard user can't drag another user into a temp

This is **expected behavior**. `Move` is admin-only by design. VX itself
does not need this — it uses self-join via OTS CoT coordination — so a
correctly-functioning VX call does not require the initiator to drag the
recipient.

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
| Per-temp ACL hook | `opentakserver/mumble/mumble_ice_app.py::DirectionEnforcementCallback.channelCreated` |
| MakeTempChannel ACL bump on persistent channels | `opentakserver/mumble/mumble_ice_app.py::MumbleIceApp._ensure_make_temp_channel_acl` |
| Direction enforcement (IN/OUT suppress flag) | `opentakserver/mumble/mumble_ice_app.py::DirectionEnforcementCallback._apply_direction` |
| Channel auto-sync from OTS groups | `opentakserver/mumble/mumble_ice_app.py::MumbleIceApp.sync_channels_from_groups` |
| Idle channel cleanup job | `opentakserver/blueprints/scheduled_jobs.py::cleanup_unmanaged_mumble_channels` |
| Murmur permission constants | `opentakserver/mumble/Murmur.ice` (search `Permission*`) |
