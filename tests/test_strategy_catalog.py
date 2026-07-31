from __future__ import annotations

import unittest

from entry.config import GLOBAL_DEFAULT_CONFIG
from services.strategy_catalog import (
    build_strategy_curves,
    strategy_payload,
    validate_strategy_selection,
)


class StrategyCatalogTests(unittest.TestCase):
    def test_catalog_exposes_current_values_and_all_families(self):
        payload = strategy_payload(GLOBAL_DEFAULT_CONFIG)
        self.assertEqual(len(payload["families"]), 4)
        self.assertEqual(payload["current"]["f_strategy"], "sensitive")
        self.assertEqual(payload["current"]["rest_strategy"], "relieved")

    def test_selection_validation_rejects_unknown_fields_and_values(self):
        self.assertEqual(
            validate_strategy_selection(
                {"f_strategy": "dull", "rest_strategy": "warmup"}
            ),
            {"f_strategy": "dull", "rest_strategy": "warmup"},
        )
        with self.assertRaises(ValueError):
            validate_strategy_selection({"S_star_init": 70})
        with self.assertRaises(ValueError):
            validate_strategy_selection({"f_strategy": "unsupported"})

    def test_function_explorer_calls_each_real_strategy_family(self):
        for family, metric in (
            ("f_strategy", "response"),
            ("C_strategy", "penalty"),
            ("rest_strategy", "delta_s"),
            ("night_strategy", "delta_e"),
        ):
            result = build_strategy_curves(
                GLOBAL_DEFAULT_CONFIG,
                family,
                stress=70,
                energy=45,
                baseline=50,
            )
            self.assertGreaterEqual(len(result["series"]), 3)
            self.assertIn(metric, result["series"][0]["points"][0])
            self.assertEqual(result["inputs"]["noise"], 0.0)


if __name__ == "__main__":
    unittest.main()
