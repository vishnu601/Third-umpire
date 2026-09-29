"""The audit log's hash chain.

Each event has its own chain (entries with no event form one more). An entry's hash is SHA-256 over the previous
entry's hash and the entry's own fields, in a fixed JSON form. Editing a row changes its hash; deleting or
inserting one breaks the next row's link. Someone who can write to the database can still rebuild the whole chain,
so the hash of the publish entry is also printed on the published results page: anyone who kept a copy can check
it later.

The migration that adds the chain imports this module, so keep it free of app imports.
"""

import hashlib
import json

GENESIS = "0" * 64


def entry_hash(entry, prev_hash):
    payload = json.dumps(
        {
            "prev": prev_hash,
            "id": entry.pk,
            "event": entry.event_id,
            "actor": entry.actor_id,
            "action": entry.action,
            "target": entry.target,
            "detail": entry.detail,
            "at": entry.created_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def seal(model, entry):
    """Link a freshly saved entry to the one before it. Call inside the transaction that inserted it: SQLite has
    one writer at a time, so no other entry can land between the insert and this update."""
    before = model.objects.filter(event_id=entry.event_id, pk__lt=entry.pk).order_by("-pk").values_list("hash", flat=True).first()
    entry.prev_hash = before or GENESIS
    entry.hash = entry_hash(entry, entry.prev_hash)
    model.objects.filter(pk=entry.pk).update(prev_hash=entry.prev_hash, hash=entry.hash)
    return entry


def verify(entries):
    """Walk one chain oldest first. Returns ok, the entry count, the head hash and the first entry that fails."""
    prev = GENESIS
    count = 0
    for entry in entries:
        count += 1
        if entry.prev_hash != prev:
            return {"ok": False, "entries": count, "head": prev, "broken_at": entry.pk, "reason": "link"}
        if entry.hash != entry_hash(entry, prev):
            return {"ok": False, "entries": count, "head": prev, "broken_at": entry.pk, "reason": "content"}
        prev = entry.hash
    return {"ok": True, "entries": count, "head": prev, "broken_at": None, "reason": ""}
