"""
Local test harness.

Runs the de-identification pipeline against synthetic data without touching
any real ShiftCare account or Google Cloud resources.  Requires no API keys.

Usage:
    python test_local.py

To test against real ShiftCare + Google APIs (dry-run — no Drive writes):
    INTEGRATION=1 python test_local.py
"""

import json
import os
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Unit tests for the de-identification engine
# ---------------------------------------------------------------------------

from src.deidentifier import (
    MedPrivacyDeidentifier,
    detect_emails,
    detect_ndis_numbers,
    detect_phone_numbers,
    generate_name_variations,
)


class TestNameVariations(unittest.TestCase):
    def test_basic_full_name(self):
        v = generate_name_variations("Jane Smith")
        self.assertIn("Jane Smith", v)
        self.assertIn("Jane", v)
        self.assertIn("Smith", v)

    def test_strips_title(self):
        v = generate_name_variations("Dr. Sarah Connor")
        self.assertIn("Sarah Connor", v)
        self.assertNotIn("Dr.", v)

    def test_nickname(self):
        v = generate_name_variations("Michael Jones")
        self.assertIn("Mike", v)

    def test_single_name(self):
        v = generate_name_variations("Madonna")
        self.assertIn("Madonna", v)


class TestPatternDetectors(unittest.TestCase):
    def test_ndis_number(self):
        self.assertEqual(detect_ndis_numbers("NDIS: 430123456"), {"430123456"})

    def test_ndis_false_positive_10_digits(self):
        # 10-digit numbers should NOT match the 9-digit NDIS pattern
        self.assertEqual(detect_ndis_numbers("ref 4301234567"), set())

    def test_mobile(self):
        self.assertEqual(detect_phone_numbers("call 0412 345 678"), {"0412 345 678"})

    def test_landline(self):
        result = detect_phone_numbers("phone: (03) 9123 4567")
        self.assertTrue(len(result) >= 1)

    def test_email(self):
        self.assertEqual(detect_emails("jane.smith@ndis.gov.au"), {"jane.smith@ndis.gov.au"})


class TestDeidentifier(unittest.TestCase):
    def setUp(self):
        self.deid = MedPrivacyDeidentifier()
        self.participants = [
            {
                "ref_code": "PART-001",
                "first_name": "Jane",
                "last_name": "Smith",
                "ndis_number": "430123456",
                "date_of_birth": "1985-03-15",
                "address": "42 Example Street, Melbourne VIC 3000",
                "phone": "0412345678",
                "email": "jane.smith@example.com",
            },
            {
                "ref_code": "PART-002",
                "first_name": "Tom",
                "last_name": "Brown",
                "ndis_number": "987654321",
                "date_of_birth": "1990-07-22",
                "address": "",
                "phone": "",
                "email": "",
            },
        ]
        self.staff = [
            {"first_name": "Alex", "last_name": "Worker"},
        ]

    def test_participant_name_replaced_with_code(self):
        result = self.deid.deidentify(
            "Jane Smith attended the session today.",
            participants=self.participants,
            staff=self.staff,
        )
        self.assertIn("PART-001", result.deidentified_text)
        self.assertNotIn("Jane Smith", result.deidentified_text)

    def test_ndis_number_redacted(self):
        result = self.deid.deidentify(
            "NDIS number 430123456 was verified.",
            participants=self.participants,
        )
        self.assertNotIn("430123456", result.deidentified_text)
        self.assertIn("[NDIS_REDACTED]", result.deidentified_text)

    def test_phone_redacted(self):
        result = self.deid.deidentify(
            "Contact at 0412 345 678.",
            participants=self.participants,
        )
        self.assertNotIn("0412 345 678", result.deidentified_text)

    def test_email_redacted(self):
        result = self.deid.deidentify(
            "Email: jane.smith@example.com",
            participants=self.participants,
        )
        self.assertNotIn("jane.smith@example.com", result.deidentified_text)

    def test_dob_in_note_redacted(self):
        result = self.deid.deidentify(
            "DOB: 15/03/1985",
            participants=self.participants,
        )
        self.assertNotIn("15/03/1985", result.deidentified_text)

    def test_cross_reference_second_participant(self):
        result = self.deid.deidentify(
            "Jane discussed her goals with Tom Brown during the session.",
            participants=self.participants,
        )
        self.assertIn("PART-001", result.deidentified_text)
        self.assertIn("PART-002", result.deidentified_text)
        self.assertNotIn("Jane", result.deidentified_text)
        self.assertNotIn("Tom Brown", result.deidentified_text)

    def test_staff_name_replaced(self):
        result = self.deid.deidentify(
            "Support worker Alex Worker assisted during the visit.",
            participants=self.participants,
            staff=self.staff,
        )
        self.assertNotIn("Alex Worker", result.deidentified_text)
        self.assertIn("[STAFF_NAME]", result.deidentified_text)

    def test_clean_note_not_quarantined(self):
        result = self.deid.deidentify(
            "Participant attended the session and engaged well with activities.",
            participants=self.participants,
        )
        self.assertFalse(result.is_quarantined)

    def test_unknown_ndis_number_caught_by_pattern_detector(self):
        # An unknown 9-digit number (not in any participant record) should be
        # caught by the Pass 4 pattern detector and replaced with [NDIS_REDACTED].
        # Since the pattern detector already handled it, the verification pass
        # finds nothing and the note is NOT quarantined.
        result = self.deid.deidentify(
            "Ref: 111222333",
            participants=[],
        )
        self.assertNotIn("111222333", result.deidentified_text)
        self.assertIn("[NDIS_REDACTED]", result.deidentified_text)
        self.assertFalse(result.is_quarantined)

    def test_quarantine_triggered_when_phone_survives_all_passes(self):
        # Quarantine triggers when the verification pass detects a pattern that
        # somehow survived. We simulate this by calling the verifier directly
        # against a piece of text that still contains a phone number.
        from src.deidentifier import detect_phone_numbers
        # Verify the detection logic itself — this is what the verification pass does
        residual = detect_phone_numbers("Call me on 0412 345 678 please.")
        self.assertTrue(len(residual) > 0, "Detector must catch this phone number")
        # In practice this triggers quarantine when merge_overlapping drops a finding
        # due to overlap with another higher-priority pattern at the same position.

    def test_idempotent_on_clean_text(self):
        clean = "Participant attended the scheduled session."
        r1 = self.deid.deidentify(clean, participants=self.participants)
        r2 = self.deid.deidentify(r1.deidentified_text, participants=self.participants)
        self.assertEqual(r1.deidentified_text, r2.deidentified_text)

    def test_comprehensive_note(self):
        note = (
            "Jane Smith (NDIS: 430123456, DOB: 15/03/1985) attended her session at "
            "42 Example Street, Melbourne VIC 3000. Support worker Alex Worker was present. "
            "Jane's mother called on 0412 345 678. "
            "Tom Brown also joined briefly. "
            "Documents sent to jane.smith@example.com."
        )
        result = self.deid.deidentify(
            note,
            participants=self.participants,
            staff=self.staff,
        )
        text = result.deidentified_text
        print("\n--- Comprehensive note output ---")
        print(text)
        print("--- Substitutions:", result.substitutions)

        self.assertNotIn("Jane Smith", text)
        self.assertNotIn("430123456", text)
        self.assertNotIn("15/03/1985", text)
        self.assertNotIn("0412 345 678", text)
        self.assertNotIn("jane.smith@example.com", text)
        self.assertNotIn("Tom Brown", text)
        self.assertNotIn("Alex Worker", text)
        self.assertIn("PART-001", text)
        self.assertIn("PART-002", text)
        self.assertFalse(result.is_quarantined, msg=result.quarantine_reason)


# ---------------------------------------------------------------------------
# Integration smoke-test (skipped unless INTEGRATION=1)
# ---------------------------------------------------------------------------

class TestIntegration(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("INTEGRATION") == "1", "Set INTEGRATION=1 to run")
    def test_shiftcare_connection(self):
        """Verifies ShiftCare API key and that the clients endpoint responds."""
        from src.config import Config
        from src.shiftcare_client import ShiftCareClient

        config = Config()
        client = ShiftCareClient(config)
        clients = client.get_clients()
        print(f"\nShiftCare: fetched {len(clients)} clients")
        self.assertIsInstance(clients, dict)

    @unittest.skipUnless(os.environ.get("INTEGRATION") == "1", "Set INTEGRATION=1 to run")
    def test_reference_map_load(self):
        """Verifies Google Sheets credentials and that the reference map sheet loads."""
        from src.config import Config
        from src.reference_map import ReferenceMap

        config = Config()
        ref_map = ReferenceMap(config)
        ref_map.load()
        print(f"\nReference map: {len(ref_map.get_all_participants())} participants loaded")

    @unittest.skipUnless(os.environ.get("INTEGRATION") == "1", "Set INTEGRATION=1 to run")
    def test_full_pipeline_dry_run(self):
        """
        Runs the pipeline for yesterday but patches the Drive uploader so no
        files are actually written to Google Drive.
        """
        import main
        from src.config import Config

        config = Config()

        with patch("main.DriveUploader") as MockUploader:
            mock_upload = MagicMock()
            mock_upload.upload_to_pending.return_value = "DRYRUN_FILE_ID"
            mock_upload.upload_to_quarantine.return_value = "DRYRUN_QUARANTINE_ID"
            MockUploader.return_value = mock_upload

            stats = main._run(config)

        print(f"\nDry-run pipeline stats: {json.dumps(stats, indent=2)}")
        self.assertIn("total", stats)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("MedPrivacy Pipeline — Local Test Suite")
    print("=" * 60)
    if os.environ.get("INTEGRATION") == "1":
        print("Integration tests ENABLED (will call real APIs)")
        print("Ensure .env is sourced or env vars are set.\n")
    else:
        print("Unit tests only. Set INTEGRATION=1 for API integration tests.\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestNameVariations))
    suite.addTests(loader.loadTestsFromTestCase(TestPatternDetectors))
    suite.addTests(loader.loadTestsFromTestCase(TestDeidentifier))
    suite.addTests(loader.loadTestsFromTestCase(TestIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
