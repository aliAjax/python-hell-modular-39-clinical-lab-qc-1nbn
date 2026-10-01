from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, utcnow
from .rules import RuleEngine, evaluate_run_entity


RUN_DISPOSITION = {
    ("accepted", True): "run_remains_accepted",
    ("accepted", False): "run_now_rejected",
    ("rejected", True): "run_now_accepted",
    ("rejected", False): "run_remains_rejected",
}
RUN_AUDIT_ACTION = {
    "run_now_rejected": "rejudge_reject",
    "run_now_accepted": "rejudge_accept",
}
BATCH_WITHDRAWN = "batch_withdrawn"
BATCH_STILL_RELEASED = "batch_still_released"


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "rejudgment" and action == "retry":
            return self.retry_rejudgment(actor, entity_id, expected_version)
        if expected_version is not None and entity["version"] != int(expected_version):
            # Check the optimistic lock before any domain rule so that a concurrent
            # commit always surfaces as a version conflict, never as a state error.
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        updated = self._apply_transition(actor, entity, action, next_status, patch, expected_version)
        if kind == "assay" and action == "change_rules":
            rejudgment = self._begin_rejudgment(actor, updated)
            return {"assay": updated, "rejudgment": rejudgment}
        return updated

    def _apply_transition(self, actor, entity, action, next_status, patch, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # -- controlled rejudgment -------------------------------------------------

    def pending_reviews(self):
        """Batches withdrawn by rejudgment that still wait for reviewer recheck."""
        return [
            batch
            for batch in self.repository.list_entities(kind="result_batch", status="intercepted")
            if batch["data"].get("review_state") == "pending"
        ]

    def rejudgment_items(self, rejudgment_id):
        record = self.get(rejudgment_id)
        if record["kind"] != "rejudgment":
            raise NotFoundError("not a rejudgment: " + rejudgment_id)
        return self.repository.list_ledger(rejudgment_id)

    def _begin_rejudgment(self, actor, assay):
        assay_id = assay["id"]
        runs = sorted(
            (
                run
                for run in self.repository.list_entities(kind="qc_run")
                if run["data"].get("assay_id") == assay_id
                and run["status"] in ("accepted", "rejected")
            ),
            key=lambda run: (str(run["data"].get("run_at", "")), run["id"]),
        )
        batches = sorted(
            (
                batch
                for batch in self.repository.list_entities(kind="result_batch")
                if batch["data"].get("assay_id") == assay_id and batch["status"] == "released"
            ),
            key=lambda batch: (str(batch["data"].get("run_at", "")), batch["id"]),
        )
        targets = [run["id"] for run in runs] + [batch["id"] for batch in batches]
        rule_version = int(assay["data"].get("rule_version", 1))
        record_id = str(uuid4())
        now = utcnow()
        record = self.repository.create_entity(
            record_id,
            "rejudgment",
            "pending",
            {
                "assay_id": assay_id,
                "rule_version": rule_version,
                "rule_config": dict(assay["data"].get("rule_config") or {}),
                "targets": targets,
                "processed": 0,
                "checkpoint": None,
                "withdrawn_batch_ids": [],
                "triggered_by": actor.user_id,
                "started_at": now,
                "finished_at": None,
                "last_error": None,
            },
            actor.user_id,
        )
        self.audit.record(record_id, actor, "rejudgment_open", None, "pending", {
            "assay_id": assay_id,
            "rule_version": rule_version,
            "target_count": len(targets),
        })
        return self._run_rejudgment(record_id, actor)

    def retry_rejudgment(self, actor, rejudgment_id, expected_version=None):
        record = self.get(rejudgment_id)
        if record["kind"] != "rejudgment":
            raise InvalidTransition("not a rejudgment entity")
        if actor.role not in ("supervisor", "admin"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        if record["status"] == "completed":
            return record
        if record["status"] not in ("pending", "failed"):
            raise InvalidTransition("rejudgment is already %s" % record["status"])
        if expected_version is not None and record["version"] != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, record["version"])
            )
        return self._run_rejudgment(rejudgment_id, actor)

    def _run_rejudgment(self, rejudgment_id, actor):
        record = self.get(rejudgment_id)
        data = dict(record["data"])
        targets = list(data.get("targets") or [])
        rule_config = dict(data.get("rule_config") or {})
        rule_version = data.get("rule_version")
        data["last_error"] = None
        record = self.repository.update_entity(record["id"], record["version"], "running", data)

        done = {item["entity_id"] for item in self.repository.list_ledger(rejudgment_id)}
        withdrawn = list(data.get("withdrawn_batch_ids") or [])
        processed = len(done)
        try:
            for target_id in targets:
                if target_id in done:
                    continue
                target = self.repository.get_entity(target_id)
                if target is None:
                    # Target vanished between plan and execution; record and move on.
                    step = {"disposition": "target_missing"}
                elif target["kind"] == "qc_run":
                    step = self._rejudge_run(actor, rejudgment_id, target, rule_config)
                elif target["kind"] == "result_batch":
                    step = self._rejudge_batch(
                        actor, rejudgment_id, target, rule_config, rule_version
                    )
                    if step["disposition"] == BATCH_WITHDRAWN:
                        withdrawn.append(target_id)
                else:
                    step = {"disposition": "target_ignored"}

                processed += 1
                checkpoint_data = dict(record["data"])
                checkpoint_data["processed"] = processed
                checkpoint_data["checkpoint"] = target_id
                checkpoint_data["withdrawn_batch_ids"] = list(withdrawn)
                self.repository.apply_rejudgment_step(
                    rejudgment_id,
                    target_id,
                    step["disposition"],
                    status=step.get("status"),
                    data=step.get("data"),
                    audit=step.get("audit"),
                    record_version=record["version"],
                    record_data=checkpoint_data,
                    record_status="running",
                )
                record = self.get(rejudgment_id)
                done.add(target_id)

            final_data = dict(record["data"])
            final_data["processed"] = processed
            final_data["checkpoint"] = None
            final_data["withdrawn_batch_ids"] = list(withdrawn)
            final_data["finished_at"] = utcnow()
            final_data["last_error"] = None
            record = self.repository.update_entity(record["id"], record["version"], "completed", final_data)
        except Exception as exc:
            committed = {item["entity_id"] for item in self.repository.list_ledger(rejudgment_id)}
            failed_data = dict(self.get(rejudgment_id)["data"])
            failed_data["processed"] = len(committed)
            failed_data["checkpoint"] = targets[len(committed) - 1] if committed else None
            failed_data["withdrawn_batch_ids"] = [
                target_id
                for target_id in targets
                if self.repository.ledger_disposition(rejudgment_id, target_id) == BATCH_WITHDRAWN
            ]
            failed_data["last_error"] = str(exc)
            failed_record = self.get(rejudgment_id)
            self.repository.update_entity(
                failed_record["id"], failed_record["version"], "failed", failed_data
            )
        return self.get(rejudgment_id)

    def _rejudge_run(self, actor, rejudgment_id, run, rule_config):
        result = evaluate_run_entity(run, self._lookup, rule_config=rule_config)
        now_accepted = bool(result["accepted"])
        disposition = RUN_DISPOSITION[(run["status"], now_accepted)]
        if disposition not in RUN_AUDIT_ACTION:
            # Outcome unchanged: no entity write, no audit, only the ledger marker.
            return {"disposition": disposition}
        next_status = "accepted" if now_accepted else "rejected"
        run_data = dict(run["data"])
        run_data["flags"] = list(result["flags"])
        run_data["z_score"] = result["z_score"]
        run_data["rule_snapshot"] = result["rule_snapshot"]
        run_data["rejudged_by"] = rejudgment_id
        return {
            "disposition": disposition,
            "status": next_status,
            "data": run_data,
            "audit": {
                "entity_id": run["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": RUN_AUDIT_ACTION[disposition],
                "from_status": run["status"],
                "to_status": next_status,
                "detail": {
                    "rejudgment_id": rejudgment_id,
                    "flags": result["flags"],
                    "z_score": result["z_score"],
                },
            },
        }

    def _rejudge_batch(self, actor, rejudgment_id, batch, rule_config, rule_version):
        run = self.repository.get_entity(batch["data"].get("qc_run_id"))
        if not run:
            return {"disposition": "batch_run_missing"}
        result = evaluate_run_entity(run, self._lookup, rule_config=rule_config)
        if result["accepted"]:
            # New rules still accept the supporting QC: keep the original release.
            return {"disposition": BATCH_STILL_RELEASED}
        batch_data = dict(batch["data"])
        batch_data["review_state"] = "pending"
        batch_data["withdrawn_by_rejudgment"] = rejudgment_id
        batch_data["withdrawn_at"] = utcnow()
        batch_data["withdraw_reason"] = (
            "supporting qc run fails under rule version %s" % rule_version
        )
        batch_data["withdraw_flags"] = list(result["flags"])
        return {
            "disposition": BATCH_WITHDRAWN,
            "status": "intercepted",
            "data": batch_data,
            "audit": {
                "entity_id": batch["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "rejudgment_withdraw",
                "from_status": "released",
                "to_status": "intercepted",
                "detail": {
                    "rejudgment_id": rejudgment_id,
                    "flags": result["flags"],
                    "released_by": batch["data"].get("released_by"),
                    "released_at": batch["data"].get("released_at"),
                },
            },
        }
