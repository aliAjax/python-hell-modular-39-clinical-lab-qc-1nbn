import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailingRepository:
    """Wrapper that fails the Nth recall_result_batch call to simulate a write crash."""

    def __init__(self, inner, fail_on_recall):
        self._inner = inner
        self._recalls = 0
        self._fail_on_recall = fail_on_recall

    def recall_result_batch(self, *args, **kwargs):
        self._recalls += 1
        if self._recalls == self._fail_on_recall:
            raise RuntimeError("injected write failure")
        return self._inner.recall_result_batch(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class RejudgmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rejudgment.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.viewer = Actor("viewer", "viewer")
        self.assay, self.lot, self.instrument = self._base()

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
                "rule_config": {"limit_sd": 3, "trend_n": 4, "consecutive_n": 4},
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

    def _run(self, value, run_at):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": self.assay["id"],
                "qc_lot_id": self.lot["id"],
                "instrument_id": self.instrument["id"],
                "value": value,
                "run_at": run_at,
            },
        )
        return self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "op"})

    def _released_batch(self, run, run_at, reviewer="rev-1"):
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": self.assay["id"],
                "instrument_id": self.instrument["id"],
                "qc_run_id": run["id"],
                "run_at": run_at,
                "patient_count": 10,
            },
        )
        return self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": reviewer})

    def _scenario(self):
        """Three runs: run1 passes both 3s and 2s; run2/run3 pass 3s but fail 2s."""
        run1 = self._run(5.0, "2026-09-27T08:00:00Z")
        run2 = self._run(5.25, "2026-09-27T09:00:00Z")
        run3 = self._run(4.75, "2026-09-27T10:00:00Z")
        batch1 = self._released_batch(run1, "2026-09-27T08:05:00Z")
        batch2 = self._released_batch(run2, "2026-09-27T09:05:00Z")
        batch3 = self._released_batch(run3, "2026-09-27T10:05:00Z")
        return run1, run2, run3, batch1, batch2, batch3

    def _recall_audit_count(self, batch_id):
        return sum(
            1
            for entry in self.service.audit_log(batch_id)
            if entry["action"] == "recall"
        )

    def test_rule_change_recalls_batches_failing_new_rules(self):
        run1, run2, run3, batch1, batch2, batch3 = self._scenario()
        rejudgment = self.service.change_assay_rules(
            self.supervisor, self.assay["id"], {"limit_sd": 2}
        )
        self.assertEqual(rejudgment["kind"], "rejudgment")
        self.assertEqual(rejudgment["status"], "completed")
        self.assertEqual(rejudgment["data"]["checkpoint_index"], 3)
        self.assertEqual(rejudgment["data"]["total_runs"], 3)
        # run2 and run3 now violate the tighter rules -> batches recalled to intercepted
        recalled = set(rejudgment["data"]["recalled_batch_ids"])
        self.assertEqual(recalled, {batch2["id"], batch3["id"]})
        for batch_id in (batch2["id"], batch3["id"]):
            batch = self.service.get(batch_id)
            self.assertEqual(batch["status"], "intercepted")
            review = batch["data"]["rejudgment_review"]
            self.assertEqual(review["status"], "pending")
            self.assertEqual(review["rejudgment_id"], rejudgment["id"])
            self.assertEqual(review["original_reviewer_id"], "rev-1")
            self.assertIn("1_3s", review["reason"])
            # original release reviewer is preserved on the batch
            self.assertEqual(batch["data"]["reviewer_id"], "rev-1")
        # run1 still passes -> batch stays released with original release info untouched
        kept = self.service.get(batch1["id"])
        self.assertEqual(kept["status"], "released")
        self.assertNotIn("rejudgment_review", kept["data"])
        self.assertEqual(kept["data"]["reviewer_id"], "rev-1")
        # assay rules were updated and history recorded
        assay = self.service.get(self.assay["id"])
        self.assertEqual(assay["data"]["rule_config"]["limit_sd"], 2)
        self.assertEqual(len(assay["data"]["rule_history"]), 1)

    def test_reconfirm_releases_recalled_batch(self):
        _, _, _, _, batch2, _ = self._scenario()
        self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        batch = self.service.get(batch2["id"])
        self.assertEqual(batch["status"], "intercepted")
        confirmed = self.service.transition(
            self.supervisor, batch["id"], "reconfirm", {}, expected_version=batch["version"]
        )
        self.assertEqual(confirmed["status"], "released")
        review = confirmed["data"]["rejudgment_review"]
        self.assertEqual(review["status"], "confirmed")
        self.assertEqual(review["confirmed_by"], self.supervisor.user_id)
        self.assertTrue(review["confirmed_at"])
        # original release reviewer retained
        self.assertEqual(confirmed["data"]["reviewer_id"], "rev-1")
        # no longer pending
        pending = self.service.pending_reviews()
        self.assertNotIn(batch["id"], [item["id"] for item in pending])

    def test_concurrent_reconfirm_one_wins_version_conflict(self):
        _, _, _, _, batch2, batch3 = self._scenario()
        self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        batch = self.service.get(batch2["id"])
        version = batch["version"]
        # reviewer A confirms first
        first = self.service.transition(
            self.supervisor, batch["id"], "reconfirm", {}, expected_version=version
        )
        self.assertEqual(first["status"], "released")
        # reviewer B submits the same review confirmation with the stale version
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, batch["id"], "reconfirm", {}, expected_version=version
            )
        # B sees the pending-review queue: batch2 is gone, batch3 still awaiting review
        pending = self.service.pending_reviews()
        pending_ids = {item["id"] for item in pending}
        self.assertNotIn(batch2["id"], pending_ids)
        self.assertIn(batch3["id"], pending_ids)

    def test_reconfirm_requires_pending_review(self):
        _, _, _, batch1, _, _ = self._scenario()
        self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        # a batch that was never recalled by a re-judgment cannot be reconfirmed
        kept = self.service.get(batch1["id"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.supervisor, kept["id"], "reconfirm", {}, expected_version=kept["version"]
            )

    def test_rule_change_permission_and_validation(self):
        self._scenario()
        with self.assertRaises(PermissionDenied):
            self.service.change_assay_rules(self.viewer, self.assay["id"], {"limit_sd": 2})
        with self.assertRaises(ValidationError):
            self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": "nope"})
        with self.assertRaises(ValidationError):
            self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": -1})

    def test_retry_from_checkpoint_no_duplicate_recall(self):
        _, _, _, batch1, batch2, batch3 = self._scenario()
        failing = FailingRepository(SQLiteRepository(self.db_path), fail_on_recall=2)
        service = DomainService(failing, RuleEngine())
        # First run: run2 recalled, run3 recall hits the injected write failure.
        with self.assertRaises(RuntimeError):
            service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        rejudgments = service.list("rejudgment")
        self.assertEqual(len(rejudgments), 1)
        rj = rejudgments[0]
        self.assertEqual(rj["status"], "failed")
        self.assertEqual(rj["data"]["checkpoint_index"], 2)
        self.assertEqual(set(rj["data"]["recalled_batch_ids"]), {batch2["id"]})
        # Retry from the checkpoint: run2 is skipped (already recalled), run3 completes.
        rj = service.run_rejudgment(self.supervisor, rj["id"])
        self.assertEqual(rj["status"], "completed")
        self.assertEqual(rj["data"]["checkpoint_index"], 3)
        self.assertEqual(set(rj["data"]["recalled_batch_ids"]), {batch2["id"], batch3["id"]})
        # batch2 recalled exactly once, batch3 recalled exactly once; batch1 untouched
        self.assertEqual(self._recall_audit_count(batch2["id"]), 1)
        self.assertEqual(self._recall_audit_count(batch3["id"]), 1)
        self.assertEqual(self._recall_audit_count(batch1["id"]), 0)
        self.assertEqual(service.get(batch1["id"])["status"], "released")
        self.assertEqual(service.get(batch2["id"])["status"], "intercepted")
        self.assertEqual(service.get(batch3["id"])["status"], "intercepted")
        # Re-running a completed re-judgment is a no-op (no extra writes/records).
        before = self._recall_audit_count(batch3["id"])
        again = service.run_rejudgment(self.supervisor, rj["id"])
        self.assertEqual(again["status"], "completed")
        self.assertEqual(self._recall_audit_count(batch3["id"]), before)

    def test_rejudgment_is_idempotent_across_rule_changes(self):
        _, _, _, _, batch2, _ = self._scenario()
        first = self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        # A second rule change creates a new re-judgment; batch2 is already intercepted,
        # so it must not be recalled again (no duplicate recall / audit record).
        second = self.service.change_assay_rules(self.supervisor, self.assay["id"], {"limit_sd": 2})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self._recall_audit_count(batch2["id"]), 1)
        self.assertEqual(self.service.get(batch2["id"])["status"], "intercepted")


if __name__ == "__main__":
    unittest.main()
