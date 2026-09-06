# Callsign whitespace handling in Mumble auth

This doc captures the state of callsign-whitespace hygiene across the OTS
write path and the Mumble authenticator's lookup path, and lists the work
that remains to make new rows immune to the same class of bug.

## TL;DR

- **Lookup side (landed in this branch):** `MumbleAuthenticator.resolve_identity`
  compares against `func.trim(EUD.callsign)` on every callsign-equality
  branch, so a stale leading or trailing space on the EUD row no longer
  prevents a Mumble client from being matched back to its OTS user.
- **Write side (follow-up PR needed):** the data ingestion paths in
  `EudHandler`, `cot_parser`, and `meshtastic_controller` assign
  `eud.callsign = contact.attrs["callsign"]` (or the equivalent) without
  stripping, so new EUD rows can still be inserted with padded callsigns.
  The trim at lookup time tolerates this, but the right defensive layer
  is also to strip on the way in.
- **Data hygiene:** existing deployments with rows already tainted by
  padded callsigns benefit from a one-time
  `UPDATE euds SET callsign = TRIM(callsign) WHERE callsign != TRIM(callsign)`
  (and the equivalent on `certificates`). Not strictly required after the
  lookup-side fix, but recommended for log readability and to keep
  display strings clean.

## The failure mode

1. ATAK publishes a self-CoT message whose `<contact callsign="Some User "/>`
   attribute carries a stray trailing (or leading) space.
2. The OTS EUD handler stores the value verbatim into `euds.callsign`.
   No `.strip()` along the way.
3. The user later connects to Murmur via the XV or VX voice plugin. The
   plugin trims the callsign client-side, replaces spaces with underscores,
   and sends `Some_User---<suffix>` as the Mumble username.
4. Murmur's pre-Ice cert check uses `nameToId(name)` (via the Ice
   authenticator) to ask OTS "does a user named `Some_User---<suffix>`
   exist?" `resolve_identity` walks its lookup chain, gets to the
   underscore-to-space branch (`spaced = "Some User"`), and compares
   against `EUD.callsign == "Some User"`. The DB row is `"Some User "`
   (with a trailing space), so the comparison fails. `resolve_identity`
   returns `None`. `authenticate()` returns `(-1, None, None)`.
5. Murmur reports the rejection as
   `<N:Some_User---<suffix>(-1)> Rejected connection from <ip>: Wrong
   certificate or password for existing user`. The error message looks
   like a TLS / cert-hash mismatch but is actually a name-lookup miss.

The lookup-side fix in this branch closes step 4 by trimming the stored
column before comparison.

## Why the lookup-side fix is the right place

The Mumble plugins (XV, VX) already trim the callsign on the client side
before constructing the Mumble username, so the search keys reaching
`resolve_identity` (the original `username`, the suffix-stripped
`base_callsign`, and the underscore-spaced `spaced`) are all clean. The
asymmetry is entirely on the stored-column side, which means a single
`func.trim(EUD.callsign) == X` swap on each comparison rescues every
affected lookup.

Trim-on-read does not modify the row, so display strings rendered from
`eud.callsign` elsewhere in OTS (mission rosters, group membership
panels, Mumble channel-creation messages) still show whatever is stored.
That's why the data cleanup is still recommended: the auth works without
it, but operators see padded names in admin UIs and logs until the
underlying row is normalized.

## Write-side follow-up (separate PR)

The following sites all write a raw `contact.attrs["callsign"]` (or
similar) straight to a callsign column. A future PR should `.strip()`
each before assignment, so new rows cannot reintroduce the bug.

| File | Line(s) | Notes |
| --- | --- | --- |
| `opentakserver/eud_handler/EudHandler.py` | 385, 687 | `eud.callsign = self.callsign` on EUD create + update; `self.callsign` is captured at line 511 from `contact.attrs["callsign"]` without trim |
| `opentakserver/cot_parser/cot_parser.py` | 244, 728 | mission-UID + marker writes both source `contact.attrs["callsign"]` |
| `opentakserver/controllers/meshtastic_controller.py` | 316, 359 | writes EUD callsign from Meshtastic device long-name |
| `opentakserver/blueprints/marti_api/mission_marti_api.py` | 862, 1040, 1987 | mission invitation and uid writes |
| `opentakserver/blueprints/ots_api/marker_api.py` | 119 | marker name from request JSON; bleach-cleaned but not stripped |

A single helper (`def _clean_callsign(s): return s.strip() if s else s`)
applied at each of these write sites is the minimal patch. The same
helper can be reused in the existing CoT validation path so future
attributes don't slip through.

## One-time data cleanup (deployment-level, optional)

```sql
BEGIN;
UPDATE euds         SET callsign = TRIM(callsign) WHERE callsign IS NOT NULL AND callsign != TRIM(callsign);
UPDATE certificates SET callsign = TRIM(callsign) WHERE callsign IS NOT NULL AND callsign != TRIM(callsign);
COMMIT;
```

Idempotent and reversible. Not required for auth to work (the lookup-side
trim handles padded rows transparently), but recommended for display
hygiene and to keep `EUD.callsign != TRIM(EUD.callsign)` a useful
invariant for future linting. The same cleanup can be packaged as an
Alembic migration once the write-side fix lands so a single upgrade
takes care of both code and data.
