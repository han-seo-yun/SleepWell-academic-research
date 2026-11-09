from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchbench.i18n import read_language, save_language


class LanguagePreferenceTests(unittest.TestCase):
    def test_first_run_missing_preference_then_language_persists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            preference = Path(folder) / "ResearchWorkbench" / "preferences.json"
            with patch("researchbench.i18n.preferences_path", return_value=preference):
                self.assertIsNone(read_language())
                save_language("ko")
                self.assertEqual(read_language(), "ko")
                save_language("en")
                self.assertEqual(read_language(), "en")
                self.assertIn('"language": "en"', preference.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
