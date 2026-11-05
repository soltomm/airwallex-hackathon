"""Randomized scenarios against the group-sales rules (see tests/fuzz_group_sales.py).

A small batch runs with the normal suite. For a bigger one:
    FUZZ_CASES=500 PYTHONPATH=src python -m pytest tests/test_group_sales_fuzz.py -q
or the script itself, which also prints coverage and a replay command:
    PYTHONPATH=src python tests/fuzz_group_sales.py --cases 500
"""
from __future__ import annotations

import os

import pytest

from fuzz_group_sales import run_case

CASES = int(os.environ.get("FUZZ_CASES", "15"))
STEPS = int(os.environ.get("FUZZ_STEPS", "60"))


@pytest.mark.parametrize("seller", ["rules", "chaos"])
def test_random_scenarios_keep_the_rules(seller):
    failures = []
    for seed in range(CASES):
        r = run_case(seed, seller, STEPS)
        if not r["ok"]:
            failures.append(f"seed {seed}: {r['error']}\n  last steps: " + " | ".join(r["trail"][-6:]))
    assert not failures, (f"{len(failures)}/{CASES} {seller} cases broke a rule; replay with "
                          f"PYTHONPATH=src python tests/fuzz_group_sales.py --seed <n> --sellers {seller} -v\n"
                          + "\n".join(failures[:5]))
