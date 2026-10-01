from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied
from .repository import utcnow
from .rules import RuleEngine, evaluate_qc, validate_rule_config


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    @staticmethod
    def _ensure_supervisor(actor):
        if actor.role not in ("supervisor", "admin"):
            raise PermissionDenied("supervisor or admin role is required")

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
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if expected_version is not None and expected != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"])
            )
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
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

    # ------------------------------------------------------------------
    # Controlled re-judgment on assay rule changes
    # ------------------------------------------------------------------

    def change_assay_rules(self, actor, assay_id, rule_config, expected_version=None):
        """Change an assay's rules and kick off a controlled re-judgment.

        The assay update uses optimistic locking; a rejudgment entity is created
        with a snapshot of the new rules and then run inline. If the run fails it
        leaves a failed rejudgment that can be resumed via run_rejudgment.
        """
        self._ensure_supervisor(actor)
        assay = self.repository.get_entity(assay_id)
        if not assay:
            raise NotFoundError("assay not found: " + assay_id)
        new_config = validate_rule_config(rule_config)
        rule_history = list(assay["data"].get("rule_history") or [])
        rule_history.append(
            {"rule_config": new_config, "changed_by": actor.user_id, "changed_at": utcnow()}
        )
        merged = dict(assay["data"])
        merged.update({"rule_config": new_config, "rule_history": rule_history})
        expected = int(expected_version) if expected_version is not None else assay["version"]
        updated = self.repository.update_entity(assay_id, expected, assay["status"], merged)
        self.audit.record(
            assay_id, actor, "change_rules", assay["status"], updated["status"],
            {"rule_config": new_config},
        )
        rejudgment = self.repository.create_entity(
            str(uuid4()),
            "rejudgment",
            "pending",
            {
                "assay_id": assay_id,
                "rule_config": new_config,
                "checkpoint_index": 0,
                "total_runs": 0,
                "verdicts": {},
                "recalled_batch_ids": [],
            },
            actor.user_id,
        )
        self.audit.record(
            rejudgment["id"], actor, "create", None, "pending",
            {"kind": "rejudgment", "assay_id": assay_id},
        )
        return self.run_rejudgment(actor, rejudgment["id"])

    def _rejudgment_runs(self, assay_id):
        runs = self.repository.find_entities("qc_run", "assay_id", assay_id)
        runs.sort(key=lambda run: (str(run["data"].get("run_at", "")), run["id"]))
        return runs

    @staticmethod
    def _history_values(run, runs):
        values = []
        run_at = str(run["data"].get("run_at", ""))
        for candidate in runs:
            if candidate["id"] == run["id"]:
                continue
            if str(candidate["data"].get("run_at", "")) >= run_at:
                continue
            if candidate["data"].get("qc_lot_id") != run["data"].get("qc_lot_id"):
                continue
            if candidate["data"].get("instrument_id") != run["data"].get("instrument_id"):
                continue
            values.append(float(candidate["data"]["value"]))
        return values

    def run_rejudgment(self, actor, rejudgment_id):
        """Process (or resume) a re-judgment from its checkpoint.

        Each QC run is re-evaluated against the snapshotted rules. Runs that now
        violate the rules recall their released batches back to intercepted.
        Progress is checkpointed after every run; re-running skips batches already
        recalled by this re-judgment, so retries never double-recall or double-write
        audit records.
        """
        self._ensure_supervisor(actor)
        rejudgment = self.repository.get_entity(rejudgment_id)
        if not rejudgment:
            raise NotFoundError("rejudgment not found: " + rejudgment_id)
        if rejudgment["kind"] != "rejudgment":
            raise ValidationError("entity is not a rejudgment: " + rejudgment_id)
        if rejudgment["status"] == "completed":
            return rejudgment
        data = dict(rejudgment["data"])
        assay_id = data.get("assay_id")
        assay = self.repository.get_entity(assay_id)
        if not assay:
            raise NotFoundError("assay not found: " + assay_id)
        # Claim the job (optimistic); a concurrent runner loses the claim.
        rejudgment = self.repository.update_entity(rejudgment["id"], rejudgment["version"], "running", data)
        runs = self._rejudgment_runs(assay_id)
        data["total_runs"] = len(runs)
        index = int(data.get("checkpoint_index", 0))
        verdicts = dict(data.get("verdicts") or {})
        recalled = list(data.get("recalled_batch_ids") or [])
        try:
            for run in runs[index:]:
                lot = self.repository.get_entity(run["data"].get("qc_lot_id"))
                if not lot:
                    raise ValidationError("qc lot not found for run: " + run["id"])
                result = evaluate_qc(
                    self._history_values(run, runs),
                    run["data"].get("value"),
                    lot["data"].get("target"),
                    lot["data"].get("sd"),
                    data.get("rule_config") or {},
                )
                verdicts[run["id"]] = {"accepted": bool(result["accepted"]), "flags": list(result["flags"])}
                if not result["accepted"]:
                    reason = (
                        "rejudgment %s: qc run %s now violates rules (%s)"
                        % (rejudgment["id"], run["id"], ", ".join(result["flags"]) or "rule violation")
                    )
                    batches = self.repository.find_entities("result_batch", "qc_run_id", run["id"])
                    for batch in batches:
                        if batch["status"] != "released":
                            continue
                        review = batch["data"].get("rejudgment_review")
                        if isinstance(review, dict) and review.get("rejudgment_id") == rejudgment["id"]:
                            continue
                        updated = self.repository.recall_result_batch(
                            batch["id"],
                            rejudgment["id"],
                            reason,
                            batch["version"],
                            actor.user_id,
                            actor.role,
                        )
                        if updated and updated["id"] not in recalled:
                            recalled.append(updated["id"])
                index += 1
                data["checkpoint_index"] = index
                data["verdicts"] = verdicts
                data["recalled_batch_ids"] = recalled
                rejudgment = self.repository.update_entity(
                    rejudgment["id"], rejudgment["version"], "running", data
                )
            rejudgment = self.repository.update_entity(
                rejudgment["id"], rejudgment["version"], "completed", data
            )
            return rejudgment
        except Exception as exc:
            data["error"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                self.repository.update_entity(rejudgment["id"], rejudgment["version"], "failed", data)
            except Exception:
                pass
            raise

    def pending_reviews(self, rejudgment_id=None):
        """Batches recalled by a re-judgment that are still awaiting review confirmation."""
        batches = self.repository.list_entities(kind="result_batch", status="intercepted")
        result = []
        for batch in batches:
            review = batch["data"].get("rejudgment_review")
            if not isinstance(review, dict) or review.get("status") != "pending":
                continue
            if rejudgment_id is not None and review.get("rejudgment_id") != rejudgment_id:
                continue
            result.append(batch)
        return result
