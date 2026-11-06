"""Ownership, immutable submission, quotas and fail-closed queue admission."""
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

from .blobs import encode
from .contracts import Configuration, digest, now


def valid_id(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid resource identifier")
    return value


class Service:
    def __init__(self, store, blobs, *, unlimited_run_owners=()):
        self.store, self.blobs = store, blobs
        self.unlimited_run_owners = frozenset(unlimited_run_owners)

    def run_limit(self, owner):
        return None if owner in self.unlimited_run_owners else 5

    def owned(self, kind, identity, owner):
        record = self.store.get(kind + "/" + valid_id(identity))
        if not record or record["owner"] != owner or record["status"] in ("deleting", "deleted"):
            raise LookupError("Resource not found")
        if record["expires_at"] <= now():
            raise LookupError("Resource expired")
        return record

    def upload(self, owner, request):
        current = [x for x in self.store.scan("uploads") if x["owner"] == owner and x["status"] != "deleted" and x["expires_at"] > now()]
        if len(current) >= 10:
            raise ValueError("Pilot limit: ten retained uploads per user")
        identity = uuid.uuid4().hex
        record = {"id": identity, "owner": owner, "filename": request.filename, "size": request.size,
                  "key": f"uploads/{identity}/source.h5ad", "status": "uploading", "created_at": now(),
                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
        key, account = "uploads/" + identity, "accounts/" + digest({"owner": owner})
        def create(values):
            count = (values[account] or {}).get("retained", 0)
            if count >= 10:
                raise ValueError("Pilot limit: ten retained uploads per user")
            return record, {key: record, account: {"retained": count + 1}}
        return self.store.atomic([key, account], create)

    def finalize(self, owner, identity):
        record = self.owned("uploads", identity, owner)
        metadata = self.blobs.metadata(record["key"])
        if metadata["size"] != record["size"]:
            raise ValueError("Uploaded size differs from declared size")
        key = "uploads/" + identity
        def change(values):
            value = values[key]
            if value["status"] != "uploading":
                return value, {}
            value.update(status="queued_inspection", input=metadata)
            return value, {key: value}
        return self.store.atomic([key], change)

    def submit(self, owner, request):
        upload = self.owned("uploads", request.upload_id, owner)
        if upload["status"] != "ready":
            raise ValueError("Wait for successful input inspection")
        if request.configuration.counts not in upload["inspection"]["matrices"]:
            raise ValueError("Selected counts were not found during inspection")
        token = hashlib.sha256((owner + ":" + request.idempotency_key).encode()).hexdigest()
        idem, quota = "idempotency/" + token, "quotas/" + digest({"owner": owner, "date": now()[:10]})
        identity = uuid.uuid4().hex
        run_key = "runs/" + identity
        fingerprint = digest({"upload": request.upload_id, "configuration": request.configuration.model_dump()})
        record = {"id": identity, "owner": owner, "upload_id": upload["id"], "input": upload["input"],
                  "configuration": request.configuration.model_dump(), "status": "queued", "stage": "queued",
                  "created_at": now(), "expires_at": upload["expires_at"]}
        def change(values):
            current_upload = values["uploads/" + upload["id"]]
            if not current_upload or current_upload["status"] != "ready" or current_upload["expires_at"] <= now():
                raise ValueError("Upload is no longer available")
            previous = values[idem]
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key was used for a different configuration")
                return {"id": previous["id"], "reused": True}, {}
            limit = self.run_limit(owner)
            if limit is not None and (values[quota] or {}).get("count", 0) >= limit:
                raise ValueError("Pilot limit: five runs per user per UTC day")
            return record, {"uploads/" + upload["id"]: {**current_upload, "run_ids": [*current_upload.get("run_ids", []), identity]}, run_key: record, idem: {"id": identity, "fingerprint": fingerprint},
                            quota: {"count": (values[quota] or {}).get("count", 0) + 1}}
        result = self.store.atomic([idem, quota, run_key, "uploads/" + upload["id"]], change)
        return self.owned("runs", result["id"], owner)

    def claim(self, kind, identity, execution):
        key, slot = kind + "/" + valid_id(identity), "control/slot"
        def change(values):
            record = values[key]
            if not record or record["status"] not in ("queued", "queued_inspection"):
                return False, {}
            if (values[slot] or {}).get("owner"):
                return False, {}
            if record["expires_at"] <= now():
                return False, {}
            record.update(status="inspecting" if kind == "uploads" else "running", execution=execution, started_at=now())
            return True, {key: record, slot: {"owner": identity, "kind": kind, "execution": execution, "acquired_at": now()}}
        return self.store.atomic([key, slot], change)

    def finish(self, kind, identity, execution, updates, release=True):
        key, slot = kind + "/" + valid_id(identity), "control/slot"
        def change(values):
            record, guard = values[key], values[slot]
            if record and record.get("execution") == execution and updates.get("status") in ("ready", "succeeded") and record.get("status") == updates["status"]:
                return record, {}
            if not record or not guard or guard.get("owner") != identity or guard.get("execution") != execution:
                raise ValueError("Processing owner changed; refuse update/release")
            record.update(updates, updated_at=now())
            changes = {key: record}
            if release:
                changes[slot] = None
            return record, changes
        return self.store.atomic([key, slot], change)

    def clear_history(self, owner):
        cleared, retained_blocked = 0, 0
        for record in self.store.scan('runs'):
            if record.get('owner') != owner or record.get('status') not in ('succeeded', 'failed', 'cancelled'):
                continue
            key = 'runs/' + valid_id(record['id'])
            def change(values):
                current, slot = values[key], values['control/slot'] or {}
                dispatch = values['control/dispatch'] or {}
                if not current or current.get('owner') != owner or current.get('status') not in ('succeeded', 'failed', 'cancelled'):
                    return (0, 0), {}
                if slot.get('owner') == current['id'] or dispatch.get('id') == current['id']:
                    return (0, 1), {}
                if current.get('history_hidden'):
                    return (0, 0), {}
                return (1, 0), {key: {**current, 'history_hidden': True}}
            count, blocked = self.store.atomic([key, 'control/slot', 'control/dispatch'], change)
            cleared += count
            retained_blocked += blocked
        return {'cleared': cleared, 'retained_blocked': retained_blocked}

    def compare(self, owner, left, right):
        if left == right:
            raise ValueError('Select two different successful runs of the same upload')
        a, b = (self.owned("runs", identity, owner) for identity in (left, right))
        if a["status"] != "succeeded" or b["status"] != "succeeded":
            raise ValueError("Compare two successful runs")
        if a["upload_id"] != b["upload_id"] or a["input"] != b["input"] or Configuration(**a["configuration"]).mappings() != Configuration(**b["configuration"]).mappings():
            raise ValueError("Comparison requires the same upload and mappings")
        from .worker import read_package
        pa, pb = read_package(self, a), read_package(self, b)
        sa, sb = set(pa["passing_barcodes"]), set(pb["passing_barcodes"])
        return {"left": left, "right": right, "left_policy": a["configuration"], "right_policy": b["configuration"],
                "left_pass": len(sa), "right_pass": len(sb), "both_pass": len(sa & sb),
                "only_left": len(sa - sb), "only_right": len(sb - sa),
                "limitation": "Technical QC comparison; does not identify a biologically optimal policy."}
