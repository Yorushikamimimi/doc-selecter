"""History restoration policy is independent of Tk and runs in the background suite."""
import unittest
from doc_selecter.gui_session import latest_restorable_plan


class PlanRestoreTests(unittest.TestCase):
    def test_preview_only_is_not_restored(self):
        self.assertIsNone(latest_restorable_plan([({'plan_id': 'preview'}, {'consumed': False})]))

    def test_executed_plan_is_restored(self):
        plan = {'plan_id': 'executed'}
        self.assertIs(latest_restorable_plan([(plan, {'consumed': True, 'phase': 'executed'})]), plan)

    def test_needs_review_plan_is_restored(self):
        for phase in ('executing', 'needs_review', 'corrupted'):
            with self.subTest(phase=phase):
                plan = {'plan_id': phase}
                self.assertIs(latest_restorable_plan([(plan, {'consumed': True, 'phase': phase})]), plan)

    def test_newest_preview_falls_back_to_earlier_execution(self):
        old = {'plan_id': 'old'}
        self.assertIs(latest_restorable_plan([({'plan_id': 'new'}, {'consumed': False}),
                                             (old, {'consumed': True})]), old)

    def test_no_plans_remains_neutral(self):
        self.assertIsNone(latest_restorable_plan([]))

    def test_consumed_is_authoritative_not_phase_name(self):
        plan = {'plan_id': 'journal-without-phase'}
        self.assertIs(latest_restorable_plan([(plan, {'consumed': True, 'phase': 'prepared'})]), plan)
        self.assertIsNone(latest_restorable_plan([(plan, {'phase': 'executed'})]))


if __name__ == '__main__':
    unittest.main()
