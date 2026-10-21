"""Offline tests for PayeeCheck. Run: python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentbank.airwallex.errors import AirwallexError
from agentbank.payee_check import SanctionsList, Verdict, check_payee, normalise

SDN = SanctionsList([("36", "AEROCARIBBEAN AIRLINES", "CUBA"), ("99", "GOLDEN STAR TRADING LLC", "SDGT")])


class FakeAPI:
    def __init__(self, error_code=None, source=None):
        self.error_code, self.source = error_code, source

    def validate_beneficiary(self, beneficiary, methods, **kw):
        if self.error_code:
            raise AirwallexError(400, "validation_failed", "",
                                 {"details": {"errors": [{"code": self.error_code, "source": self.source}]}})
        return "OK"


def payee(name="Acme Freight Inc", **bank):
    return {"company_name": name, "bank_details": {"account_name": name, "bank_country_code": "US", **bank}}


class PayeeCheckTests(unittest.TestCase):
    def test_normalise_drops_suffixes_and_punctuation(self):
        self.assertEqual(normalise("Golden Star Trading, L.L.C."), "GOLDEN STAR TRADING L L C")
        self.assertEqual(normalise("The Acme Co. Ltd"), "ACME")

    def test_screening_exact_fuzzy_and_clear(self):
        self.assertEqual(SDN.screen("Golden Star Trading")[0].score, 1.0)
        self.assertGreaterEqual(SDN.screen("Aero Caribean Airline Ltd")[0].score, 0.88)
        self.assertEqual(SDN.screen("Acme Freight Inc"), [])

    def test_verdicts(self):
        self.assertIs(check_payee(FakeAPI(), SDN, payee(), ["LOCAL"]).verdict, Verdict.PASS)
        bad_iban = check_payee(FakeAPI("083", "beneficiary.bank_details.iban"), SDN, payee(), ["SWIFT"])
        self.assertIs(bad_iban.verdict, Verdict.FAIL)
        self.assertIn("checksum", bad_iban.bank_problems[0])
        self.assertIs(check_payee(FakeAPI(), SDN, payee("Aerocaribbean Airlines"), ["LOCAL"]).verdict, Verdict.FAIL)
        self.assertIs(check_payee(FakeAPI(), SDN, payee("Aero Caribean Airline"), ["LOCAL"]).verdict, Verdict.REVIEW)

    def test_swift_code_checked_locally(self):
        wrong_country = check_payee(FakeAPI(), SDN, payee(bank_country_code="DE", swift_code="BARCGB22XXX"), ["SWIFT"])
        self.assertIs(wrong_country.verdict, Verdict.FAIL)
        malformed = check_payee(FakeAPI(), SDN, payee(swift_code="NOT-A-BIC"), ["SWIFT"])
        self.assertIn("malformed", malformed.bank_problems[0])


if __name__ == "__main__":
    unittest.main()
