import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailingOnceRepository(SQLiteRepository):
    """Fails during the nth rejudgment step after its write transaction has
    begun, so ledger/entity/audit/checkpoint roll back atomically."""

    def __init__(self, path, fail_at_call=2):
        super().__init__(path)
        self.calls = 0
        self.fail_at_call = fail_at_call

    def apply_rejudgment_step(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self.fail_at_call:
            original_connect = self._connect

            def sabotaged_connect():
                connection = original_connect()
                original_execute = connection.execute

                def execute(sql, *a, **k):
                    if sql == "BEGIN IMMEDIATE":
                        original_execute(sql, *a, **k)
                        raise RuntimeError("simulated storage outage")
                    return original_execute(sql, *a, **k)

                connection.execute = execute
                return connection

            self._connect = sabotaged_connect
            try:
                return super().apply_rejudgment_step(*args, **kwargs)
            finally:
                self._connect = original_connect
        return super().apply_rejudgment_step(*args, **kwargs)


class ControlledRejudgmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "rejudge.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.reviewer_a = Actor("reviewer-a", "supervisor")
        self.reviewer_b = Actor("reviewer-b", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _assay_lot_instrument(self, rule_config=None):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
                "rule_config": rule_config or {"limit_sd": 3, "trend_n": 4, "consecutive_n": 4},
            },
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, lot, instrument

    def _evaluated_run(self, assay, lot, instrument, value, run_at):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        return self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})

    def _release_batch(self, assay, instrument, run, run_at):
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": 5,
            },
        )
        return self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "orig-reviewer"})

    def test_rule_change_withdraws_only_newly_failing_batches(self):
        assay, lot, instrument = self._assay_lot_instrument()
        # z=2.5 passes under 3s, fails under 2s.
        bad_run = self._evaluated_run(assay, lot, instrument, 5.25, "2026-09-27T08:00:00Z")
        bad_batch = self._release_batch(assay, instrument, bad_run, "2026-09-27T08:05:00Z")
        # z=0.2 passes under both.
        good_run = self._evaluated_run(assay, lot, instrument, 5.02, "2026-09-27T09:00:00Z")
        good_batch = self._release_batch(assay, instrument, good_run, "2026-09-27T09:05:00Z")
        self.assertEqual(bad_batch["status"], "released")

        outcome = self.service.transition(
            self.supervisor,
            assay["id"],
            "change_rules",
            {
                "rule_config": {"limit_sd": 2, "trend_n": 4, "consecutive_n": 4},
                "reason": "tighten single-point limit per new validation",
            },
        )
        updated_assay, record = outcome["assay"], outcome["rejudgment"]
        self.assertEqual(updated_assay["data"]["rule_version"], 2)
        self.assertEqual(record["status"], "completed")

        withdrawn = self.service.get(bad_batch["id"])
        self.assertEqual(withdrawn["status"], "intercepted")
        self.assertEqual(withdrawn["data"]["review_state"], "pending")
        # Original release provenance is preserved for the audit trail.
        self.assertEqual(withdrawn["data"]["released_by"], "qc-supervisor")
        self.assertIn("released_at", withdrawn["data"])
        self.assertEqual(withdrawn["data"]["withdraw_flags"], ["1_3s"])

        untouched = self.service.get(good_batch["id"])
        self.assertEqual(untouched["status"], "released")
        self.assertEqual(untouched["version"], good_batch["version"])
        self.assertEqual(untouched["updated_at"], good_batch["updated_at"])
        self.assertNotIn("review_state", untouched["data"])

        # Reviewers see exactly the withdrawn batch in the recheck queue.
        queue = self.service.pending_reviews()
        self.assertEqual([item["id"] for item in queue], [bad_batch["id"]])

        bad_run_after = self.service.get(bad_run["id"])
        self.assertEqual(bad_run_after["status"], "rejected")

    def test_reconfirm_after_recheck_releases_with_optimistic_lock(self):
        assay, lot, instrument = self._assay_lot_instrument()
        run = self._evaluated_run(assay, lot, instrument, 5.25, "2026-09-27T08:00:00Z")
        batch = self._release_batch(assay, instrument, run, "2026-09-27T08:05:00Z")
        self.service.transition(
            self.supervisor,
            assay["id"],
            "change_rules",
            {"rule_config": {"limit_sd": 2}, "reason": "tighten"},
        )
        withdrawn = self.service.get(batch["id"])
        self.assertEqual(withdrawn["version"], batch["version"] + 1)

        # Before recheck the batch must be retested with a fresh accepted QC run;
        # the corrective retest keeps it in interception, awaiting the recheck.
        retest_run = self._evaluated_run(assay, lot, instrument, 5.01, "2026-09-27T10:00:00Z")
        withdrawn = self.service.transition(
            self.supervisor,
            withdrawn["id"],
            "retest",
            {"replacement_run_id": retest_run["id"], "reason": "corrective rerun after withdrawal"},
        )
        self.assertEqual(withdrawn["status"], "intercepted")
        self.assertEqual(withdrawn["data"]["review_state"], "pending")

        # Two reviewers submit the recheck concurrently against the same version.
        first = self.service.transition(
            self.reviewer_a,
            withdrawn["id"],
            "reconfirm",
            {"reviewer_id": "reviewer-a", "resolution": "corrective retest passed"},
            expected_version=withdrawn["version"],
        )
        self.assertEqual(first["status"], "released")
        self.assertEqual(first["data"]["review_state"], "confirmed")
        self.assertEqual(first["data"]["reconfirmed_by"], "reviewer-a")
        # Original release metadata survives the full cycle.
        self.assertEqual(first["data"]["released_by"], "qc-supervisor")
        self.assertIn("released_at", first["data"])

        with self.assertRaises(ConflictError) as collision:
            self.service.transition(
                self.reviewer_b,
                withdrawn["id"],
                "reconfirm",
                {"reviewer_id": "reviewer-b", "resolution": "I confirm too"},
                expected_version=withdrawn["version"],
            )
        self.assertIn("version conflict", str(collision.exception))

        # Losing reviewer refreshes: item is gone from the queue, audit shows one confirm only.
        self.assertEqual(self.service.pending_reviews(), [])
        reconfirm_actions = [
            entry
            for entry in self.service.audit_log(batch["id"])
            if entry["action"] == "reconfirm"
        ]
        self.assertEqual(len(reconfirm_actions), 1)
        self.assertEqual(reconfirm_actions[0]["actor_id"], "reviewer-a")

    def test_rejudgment_resumes_from_checkpoint_without_duplicate_writes(self):
        failing = FailingOnceRepository(
            Path(self.tmp.name) / "flaky.db", fail_at_call=2
        )
        service = DomainService(failing, RuleEngine())
        self.service = service

        assay, lot, instrument = self._assay_lot_instrument()
        bad_run = self._evaluated_run(assay, lot, instrument, 5.25, "2026-09-27T08:00:00Z")
        bad_batch = self._release_batch(assay, instrument, bad_run, "2026-09-27T08:05:00Z")
        good_run = self._evaluated_run(assay, lot, instrument, 5.02, "2026-09-27T09:00:00Z")
        good_batch = self._release_batch(assay, instrument, good_run, "2026-09-27T09:05:00Z")

        outcome = service.transition(
            self.supervisor,
            assay["id"],
            "change_rules",
            {"rule_config": {"limit_sd": 2}, "reason": "tighten"},
        )
        record = outcome["rejudgment"]
        self.assertEqual(record["status"], "failed")
        self.assertIsNotNone(record["data"]["checkpoint"])
        self.assertEqual(record["data"]["processed"], 1)

        # The first target (the newly rejected QC run) committed with its audit.
        self.assertEqual(service.get(bad_run["id"])["status"], "rejected")
        reject_audits = [
            entry for entry in service.audit_log(bad_run["id"]) if entry["action"] == "rejudge_reject"
        ]
        self.assertEqual(len(reject_audits), 1)

        # The failing second step rolled back atomically: no withdraw write/audit yet.
        self.assertEqual(service.get(bad_batch["id"])["status"], "released")
        self.assertEqual(
            len([
                entry
                for entry in service.audit_log(bad_batch["id"])
                if entry["action"] == "rejudgment_withdraw"
            ]),
            0,
        )

        # Resume from the breakpoint; already-recorded targets are skipped by the ledger.
        resumed = service.retry_rejudgment(self.supervisor, record["id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertGreater(resumed["version"], record["version"])
        self.assertEqual(resumed["data"]["processed"], 4)

        items = {item["entity_id"]: item["disposition"] for item in service.rejudgment_items(record["id"])}
        self.assertEqual(items[bad_run["id"]], "run_now_rejected")
        self.assertEqual(items[bad_batch["id"]], "batch_withdrawn")
        self.assertEqual(items[good_run["id"]], "run_remains_accepted")
        self.assertEqual(items[good_batch["id"]], "batch_still_released")
        self.assertEqual(len(items), 4)

        withdrawn = service.get(bad_batch["id"])
        self.assertEqual(withdrawn["status"], "intercepted")
        withdraw_audits_after = [
            entry
            for entry in service.audit_log(bad_batch["id"])
            if entry["action"] == "rejudgment_withdraw"
        ]
        self.assertEqual(len(withdraw_audits_after), 1)
        # The already-committed first target was not processed again.
        self.assertEqual(
            len([e for e in service.audit_log(bad_run["id"]) if e["action"] == "rejudge_reject"]),
            1,
        )

        # Retrying a completed rejudgment is a no-op (no extra writes or audit).
        again = service.retry_rejudgment(self.supervisor, resumed["id"])
        self.assertEqual(again["version"], resumed["version"])
        self.assertEqual(
            len([e for e in service.audit_log(bad_batch["id"]) if e["action"] == "rejudgment_withdraw"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
