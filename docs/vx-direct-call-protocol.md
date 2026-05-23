# ATAK VX direct-call protocol — what we know

Reverse-engineered from live observation of the official VX voice plugin
(plain VX **v2.1.0**) against an OpenTAKServer + Murmur 1.3.4 deployment.
In murmur's `slog` and TLS logs these clients identify as
`1.0.0 (ATAK: ATAK_Vx)` — that's the Mumble protocol compatibility version
the client advertises, NOT the VX plugin's own release version.
Not from VX source — treat as "best-current-understanding" not gospel.

## Architecture: signaling + media in two separate layers

VX direct calls look superficially like Mumble channels but are really
two independent layers stacked together, similar to SIP signaling +
RTP media:

```
Layer 1 — CHANNEL HANDSHAKE (Mumble protocol)
   Caller creates a temporary channel "TAK PRIVATE - <hex>"
   Caller calls setACL on it (to lock down the call)
   Recipient moves into the temp as a visual handshake
   Visible: server-side via Murmur slog / Ice events

Layer 2 — AUDIO (Mumble VoiceTarget / whisper)
   Speaker configures a numbered VoiceTarget pointing at the recipient
   Voice packets transmit with that target id — server routes only to that user
   NOT visible server-side (UDP, no slog entries for voice routing)
```

**Critical implication:** the audio doesn't flow through the temp channel.
In actual call captures we observed the caller stay in their original group
channel while the recipient moved into the temp, and the call still worked.
The temp is a visual + handshake indicator; the actual voice is whisper.

## Layer 1: Channel handshake (Mumble protocol)

Observed sequence when a call initiates (from `mumble-server.log`):

```
T+0.000s  CALLER  Added channel "TAK PRIVATE - <hex>" under Root
T+0.160s  CALLER  Updated ACL in channel TAK PRIVATE - <hex>
T+2-7s    CALLEE  Moved CALLEE to TAK PRIVATE - <hex>
... audio flows via whisper (see Layer 2) ...
T+N s     CALLER  Moved CALLER to Root (end-call intermediate)
T+N+50ms  CALLER  Moved CALLER to original_channel (REACT/Family/etc.)
T+N s     CALLEE  Moved CALLEE to original_channel
T+N s     Murmur  auto-GCs the empty temp channel
```

### Naming convention

Two formats observed: `TAK PRIVATE - 9a265fc4` (8 hex chars) and
`TAK PRIVATE - 452b73ccc87f` (12 hex chars). Likely a session UUID truncated
to 8 vs 12 chars; possibly distinguishing 1:1 vs conference. **Other VX
clients recognize this prefix as a call channel** and apply special UI
behavior (don't show in normal channel list).

### Murmur auto-creates a creator-admin group

This is the most important detail for OTS-side ACL design. When **any** user
creates a temp via the Mumble protocol, Murmur **automatically creates a
local `admin` group on that temp** and adds the creator's userid to it:

```
groups=[..., admin/inh=0/inheritable=1/add=[<creator_uid>]/members=[<creator_uid>]]
```

Combined with Root's inherited `admin/allow=0x707ff` ACL, this gives the
creator full admin powers ON THEIR OWN TEMP, even if they aren't a real
server admin. **That's how non-admin standard users successfully `setACL`
on their own call channel** at T+0.160s. The `Updated ACL` line in the
murmur log is VX configuring its own call's permissions (likely limiting
who can enter), which requires `Write` on the temp — granted via the
creator-admin chain.

**Do not destroy this group.** Specifically: don't call `setACL` on the
temp with `groups=[], inherit=False` — that wipes the creator-admin group
and breaks VX's call lifecycle. Instead, **don't intervene per-temp at
all** and instead put an `@auth` grant on parent channels with
`apply_sub=True` so it inherits down to temps (see "OTS server-side
strategy" below).

### What ACL does VX set on its own temp?

Unknown — VX issues `setACL` ~160ms after creation but it's a client-side
Mumble protocol message; the server log only shows "Updated ACL in channel"
with no contents. We never captured the actual ACL VX configures because
our diagnostic `getACL` calls were timing out under load (Ice 30s).

**To recover this:** run `tcpdump` on port 64738 during a call and decode
the Mumble protocol `ACL` message in the capture. Or hook a `ServerCallback`
implementation that does `getACL` immediately after `channelStateChanged`
on temp channels.

## Layer 2: Audio path via VoiceTarget (whisper)

Murmur supports per-client "VoiceTarget" configurations — numbered slots
(1-30) that map a target id to a list of `session ids` and/or `channel ids`.
When the client transmits voice with a target byte set, Murmur routes the
audio only to the targets, **not** to the client's current channel.

This is the actual audio path for VX direct calls. Evidence from observed
calls: the caller often **never moves into their own temp channel** — they
create it, the recipient joins the temp, and audio flows the entire time
without the caller leaving their original group channel. The only way that
works is whisper.

### Server-side permissions needed for whisper to work

| Permission | Where applied | Why |
|---|---|---|
| `Whisper` (0x100) | Caller's source channel | Server checks sender has whisper rights in their current channel before routing voice |
| `Speak` (0x08) | Caller's source channel | Implied — `suppress`ed users can't transmit any voice including whisper |
| User NOT `suppress`ed | Anywhere | Server-mute blocks ALL transmission including whisper |
| `Enter` on the temp | For recipients | Needed so they can move into the temp for the visual handshake |

**The suppress flag is the most likely server-side blocker for VX calls.**
A "listen-only" mode that uses Murmur's suppress flag will silently break
VX whisper-based calls for the suppressed user — they can hear but cannot
speak in any channel. If a future deployment ever needs a listen-only
restriction that coexists with VX, use channel-level ACL `Speak` revocation
instead of the suppress flag, so the user is gagged in a specific channel
but free to whisper from elsewhere. OTS itself does not currently set the
suppress flag.

## End-call behavior (caller stuck-in-Root pattern)

End-call typically transitions through these states:

```
1. Caller's UI hits End Call
2. Caller's VX clears its VoiceTarget configuration (audio stops)
3. Caller's VX moves the caller to Root (intermediate state)
4. ~30 seconds later: plain VX (v2.1.0) drops its TLS socket and reconnects
5. New session lands in Root (Murmur default for fresh connections)
6. Eventually VX moves the user to their original channel
   (observed delays: 35ms to 60+ seconds)
```

**Gotcha:** between steps 4 and 6, the user appears stuck in Root for up
to a minute. This is VX client-side, not a server issue. If the server
returns `PermissionDenied` for any of the VX-issued channel ops during
this window (e.g., because a setACL races), the Mumble client surfaces
an error and Android-localized error strings with unfilled `%1$d`
placeholders ("Cannot rejoin %1$d") become visible — that's a client
bug surfacing because the server didn't provide the parameter, not a
real permission issue.

**To mask the stuck-in-Root delay server-side:** auto-move OTS-authenticated
users to their preferred OTS group channel right after `authenticate()`
returns. Requires a "primary group" concept or persisting last-channel
per user. Optional; trades complexity for UX polish.

## OTS server-side strategy: inheritance, not per-temp setACL

We tried setACL on every temp via a `channelCreated` callback. Bad idea:

- Every temp triggered `getACL` + `setACL` on a background thread
- Plus a 1.5s-delayed diagnostic `getACL`
- Under VX's typical call lifecycle (multiple temps, several users moving
  channels), this overlapped with Murmur's own state broadcasts
- Murmur stalled on lock contention → 30s+ Ice timeouts cascaded
- User-visible: 30-60 second End-Call hangs, `%1$d` client errors

**Better strategy:** put the `@auth` grant on the PARENT channels with
`applySubs=True` so it propagates to temps via inheritance:

```python
# On Root: existing @auth grant gets apply_sub=True
# On each OTS group channel: ADD a new @auth ACL entry with
#   applyHere=False  (channel itself stays members-only)
#   applySubs=True   (temps under it inherit)
#   allow=0x30e      (Traverse|Enter|Speak|Whisper|TextMessage)
```

Result: zero Ice calls during call lifecycle. Murmur's defaults (creator-admin
group + Root admin inheritance) handle the per-temp permissions. Cross-group
conferences work because any auth user inherits Enter+Speak on temps
regardless of which OTS group they belong to.

## Permission matrix users actually need

For VX direct calls to work end-to-end:

| Permission | Standard user (own OTS group) | Standard user as creator of temp | Standard user as callee on someone's temp | OTS admin |
|---|---|---|---|---|
| `Traverse` (see channel) | ✓ via `@all` | ✓ via `@all` (Root inheritance) | ✓ | ✓ |
| `Enter` (join channel) | ✓ via `<groupname>` | ✓ via auto creator-admin | ✓ via `@auth` apply_sub from parent | ✓ via admin |
| `Speak`, `Whisper`, `TextMessage` | ✓ via `<groupname>=0x70e` | ✓ via creator-admin | ✓ via `@auth` apply_sub | ✓ via admin |
| `MakeTempChannel` (0x400) | ✓ via `<groupname>` and `@auth` on Root | ✓ via creator-admin | n/a | ✓ via admin |
| `Move` (drag other users) | ✗ — admin-only by design | ✓ on their own temp via creator-admin (needed for VX to manage participants) | ✗ | ✓ via admin |
| `Write` (manage channel ACL) | ✗ | ✓ on their own temp via creator-admin (needed for VX's "Updated ACL" call) | ✗ | ✓ via admin |
| `Kick`/`Ban`/`Register` | ✗ | ✗ | ✗ | ✓ via admin |

**Key insight:** `Move` and `Write` for non-admins are NOT granted at the
OTS group level. They come for free per-temp via Murmur's automatic
creator-admin group. So creators are "admins of their own call" without
being server admins.

## Operational quirks to design around

| Quirk | What you'll see | Mitigation |
|---|---|---|
| Plain VX (v2.1.0) reconnect cycle | All TLS sockets drop simultaneously every few minutes, immediate reconnect | None server-side |
| Caller doesn't enter own temp | Murmur log shows creator stays in original channel; only callee moves | Don't worry about it; audio is whispered from origin |
| `%1$d` errors in client | Localization fallback when server returns slow response with int param | Fix the slowness (Ice load, lock contention) not the string |
| `not allowed to Write ACL` denials | VX's own setACL failing | Your code wiped the creator-admin group. Don't pass `groups=[]` |
| Caller stuck in Root after call | New session lands in Root; VX is slow to move | Optional: server-side auto-move-on-auth |
| Channel `[id:0*]` notation | `*` indicates `temporary=true` | Use `Channel.temporary` field via Ice, not the log notation |

## Open questions still worth answering

| Question | How to find out |
|---|---|
| Exact ACL VX configures via "Updated ACL" | tcpdump port 64738 during call, decode protocol; or hook Murmur callback for `getACL` immediately after temp creation |
| 8-char vs 12-char hex suffix in `TAK PRIVATE - <hex>` | Initiate a 1:1 vs a 3-way and compare |
| How does VX signal call setup between clients? | Decode Mumble plugin-data messages during a call; not visible in slog |
| Does VX use Mumble TextMessage as a fallback signaling channel? | Hook `userTextMessage` callback; observe |
| How does VX handle calls when both parties are on different Mumble servers? | Likely doesn't |

## References

- Murmur Ice slice: `/usr/share/slice/Murmur.ice` or the project's
  `opentakserver/mumble/Murmur.ice`
- Mumble protocol reference: <https://mumble-protocol.readthedocs.io/>
- Mumble permission bit values (search `Permission*` constants in Murmur.ice):
  Write=0x01, Traverse=0x02, Enter=0x04, Speak=0x08, MuteDeafen=0x10,
  Move=0x20, MakeChannel=0x40, LinkChannel=0x80, Whisper=0x100,
  TextMessage=0x200, MakeTempChannel=0x400,
  Kick=0x10000, Ban=0x20000, Register=0x40000
