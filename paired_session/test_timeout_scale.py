import os
import unittest
from unittest.mock import patch

from paired_session import timeout_scale as tsc


class TimeoutScaleTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(tsc.ENV, None)

    def test_the_test_factor_follows_load_per_cpu_within_one_and_six(self):
        with patch.object(os, 'cpu_count', return_value=8):
            for load, expected in ((2.0, 1.0), (24.0, 3.0), (400.0, 6.0)):
                with self.subTest(load=load), patch.object(os, 'getloadavg', return_value=(load, 0.0, 0.0)):
                    self.assertEqual(tsc.factor(), expected)
                    self.assertEqual(tsc.scaled(5), 5 * expected)
            with patch.object(os, 'getloadavg', return_value=(24.5, 0.0, 0.0)):
                self.assertEqual(tsc.scaled_arg(10), '31')                   # rounded up to a whole second

    def test_the_override_wins_and_is_clamped(self):
        with patch.object(os, 'getloadavg', return_value=(400.0, 0.0, 0.0)):
            for value, expected in (('2.5', 2.5), ('0.1', 1.0), ('50', 6.0), ('nan', 1.0), ('junk', 1.0)):
                with self.subTest(value=value):
                    os.environ[tsc.ENV] = value
                    self.assertEqual(tsc.factor(), expected)
                    self.assertEqual(tsc.env_factor(), expected)

    def test_the_product_factor_is_one_unless_the_override_is_set(self):
        with patch.object(os, 'getloadavg', return_value=(400.0, 0.0, 0.0)) as load:
            self.assertEqual(tsc.env_factor(), 1.0)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
