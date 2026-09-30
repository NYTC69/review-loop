import unittest
from types import MappingProxyType

from paired_session.budget_policy import BUDGET_CAPS, BudgetExtension, validate_extension


class BudgetPolicyTests(unittest.TestCase):
    def test_stage_caps_and_bounded_extensions(self):
        self.assertEqual(validate_extension('EXEC', 6, 2, 0, 0, 0), BudgetExtension(8, 0, 0))
        self.assertEqual(validate_extension('run-calls', 192, 16, 7, 48, 0),
                         BudgetExtension(208, 16, 0))
        self.assertEqual(validate_extension('epochs', 15, 1, 7, 0, 3), BudgetExtension(16, 0, 1))
        self.assertEqual(BUDGET_CAPS['SECURITY'], (3, 5))
        self.assertIsInstance(BUDGET_CAPS, MappingProxyType)
        with self.assertRaises(TypeError):
            BUDGET_CAPS['EXEC'] = (6, 100)

    def test_hard_ceiling_and_per_command_increment_are_fixed(self):
        with self.assertRaisesRegex(ValueError, 'below its fixed default'):
            validate_extension('EXEC', 5, 1, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'hard ceiling'):
            validate_extension('EXEC', 8, 1, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'per-command cap'):
            validate_extension('DOCS', 7, 3, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'per-command cap'):
            validate_extension('run-calls', 192, 17, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'positive'):
            validate_extension('EXEC', 6, 0, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'per-item'):
            validate_extension('run-calls', 192, 16, 7, 60, 0)
        with self.assertRaisesRegex(ValueError, 'per-item'):
            validate_extension('epochs', 15, 1, 7, 0, 4)
        with self.assertRaisesRegex(ValueError, 'recorded item total'):
            validate_extension('run-calls', 208, 1, 0, 15, 0)
        with self.assertRaisesRegex(ValueError, 'recorded item total'):
            validate_extension('epochs', 16, 1, 0, 0, 0)

    def test_item_limits_and_unknown_stages_fail_closed(self):
        for values in ((8, 0, 0), (0, 65, 0), (0, 0, 5)):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'per-item'):
                validate_extension('EXEC', 6, 1, *values)
        with self.assertRaisesRegex(ValueError, 'unknown budget stage'):
            validate_extension('not-a-stage', 0, 1, 0, 0, 0)
        for stage in ('item-calls', 'rejects'):
            with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, 'parent or is fixed'):
                validate_extension(stage, BUDGET_CAPS[stage][0], 1, 0, 0, 0)

    def test_booleans_and_negative_accounting_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'integers'):
            validate_extension('EXEC', True, 1, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'integers'):
            validate_extension('EXEC', 6, 1.0, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'integers'):
            validate_extension('EXEC', 6, '1', 0, 0, 0)
        with self.assertRaisesRegex(ValueError, 'nonnegative'):
            validate_extension('EXEC', 6, 1, 0, -1, 0)


if __name__ == '__main__':
    unittest.main()
