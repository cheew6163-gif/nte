import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from yihuan.privacy import redact_frame, redact_report, redact_text
from yihuan.probe import ProbeOptions, ProbeReport, run_input_check, _desktop_frame
from yihuan.vision import read_image, save_image

ROOT = Path(__file__).resolve().parent.parent


class PrivacyTests(unittest.TestCase):
    def test_account_template_and_matching_spec_are_removed(self):
        spec = json.loads((ROOT/"assets999/templates.json").read_text(encoding="utf-8"))
        self.assertNotIn("hud", spec["templates"])
        from yihuan.nte999 import Detector999
        self.assertNotIn("hud", Detector999(ROOT/"assets999").templates)
        self.assertEqual(read_image(ROOT/"assets999/templates/hud.png").shape, (1,1,3))

    def test_saving_evidence_hides_account_bar_and_names_without_mutating_capture(self):
        original = np.full((843,1499,3), 123, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"evidence.png"
            save_image(path, original, redact=True, scene="匹配页")
            saved = read_image(path)
        self.assertTrue(np.all(saved[820:840,10:250] == 0))
        self.assertTrue(np.all(saved[125:165,240:500] == 0))
        self.assertTrue(np.all(saved[300:350,700:800] == 123))
        self.assertTrue(np.all(original == 123))

    def test_settlement_and_battle_names_are_masked(self):
        frame = np.full((843,1499,3), 255, dtype=np.uint8)
        for scene, (x,y) in (("胜利结算",(180,410)),("失败结算",(1300,410)),
                              ("离开确认框",(1200,70)),("战场",(260,70))):
            with self.subTest(scene=scene):
                result = redact_frame(frame,scene)
                self.assertTrue(np.all(result[y,x] == 0))

    def test_redaction_scales_to_4k_and_keeps_bgra_masks_opaque(self):
        frame = np.full((2160,3840,4), 127, dtype=np.uint8)
        result = redact_frame(frame,"地图")
        self.assertTrue(np.all(result[2140,20,:3] == 0))
        self.assertEqual(result[2140,20,3],255)

    def test_unknown_scene_hides_all_known_name_fields(self):
        frame = np.full((843,1499,3),255,dtype=np.uint8)
        result = redact_frame(frame,"未识别")
        self.assertTrue(np.all(result[410,180] == 0))
        self.assertTrue(np.all(result[70,1200] == 0))

    def test_raw_capture_without_window_cannot_capture_other_applications(self):
        with self.assertRaisesRegex(ValueError,"目标窗口"):
            _desktop_frame(None)

    def test_reports_remove_personal_paths_and_email_but_keep_error_evidence(self):
        raw = {"error":5,"runtime":{"executable":r"C:\Users\example_user\project\.venv\python.exe"},
               "details":["/home/example_user/project", "example_user@example.test"]}
        saved = redact_report(raw)
        text = json.dumps(saved)
        self.assertNotIn("example_user",text)
        self.assertEqual(saved["error"],5)
        self.assertIn(".venv",text)
        self.assertIn("example_user",raw["runtime"]["executable"])

    def test_uid_and_structured_account_fields_are_generic(self):
        saved = redact_report({"uid":123456789,"nickname":"sample_alias",
                               "nested":{"email":"sample@example.test"},
                               "message":"UID: 123456789", "error_code":5})
        self.assertNotIn("123456789",json.dumps(saved))
        self.assertNotIn("sample_alias",json.dumps(saved))
        self.assertEqual(saved["error_code"],5)
        self.assertEqual(saved["uid"],"<private>")

    def test_private_directory_with_spaces_is_fully_redacted(self):
        text=redact_text(r'C:\Users\Example User\project\run.py')
        self.assertNotIn("Example",text)
        self.assertNotIn("User\\project",text)
        self.assertIn("project",text)

    def test_logging_redacts_exception_tracebacks(self):
        import logging
        from yihuan.ui import PrivacyFormatter
        try:
            raise RuntimeError(r'C:\Users\Example User\project UID: 123456789 sample@example.test')
        except RuntimeError:
            import sys
            record=logging.LogRecord("yihuan",logging.ERROR,__file__,1,"failed",(),sys.exc_info())
        rendered=PrivacyFormatter("%(message)s").format(record)
        self.assertNotIn("Example User",rendered)
        self.assertNotIn("123456789",rendered)
        self.assertNotIn("sample@example.test",rendered)
        self.assertIn("RuntimeError",rendered)

    def test_probe_report_writes_sanitized_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            report = ProbeReport(ProbeOptions("inspect",output_dir=Path(directory)))
            report.data["sample"] = r"C:\Users\example_user\project"
            saved = report.finish().read_text(encoding="utf-8")
        self.assertNotIn("example_user",saved)

    def test_environment_inspection_no_longer_captures_full_desktop(self):
        class Api:
            def input_diagnostics(self, **_): return {"desktop": {}}
        with tempfile.TemporaryDirectory() as directory, \
             patch("yihuan.probe.WinApi",return_value=Api()), \
             patch("yihuan.probe._desktop_frame",side_effect=AssertionError("full desktop capture forbidden")) as capture, \
             patch("builtins.print"):
            self.assertEqual(run_input_check(ProbeOptions("inspect",output_dir=Path(directory))),0)
            capture.assert_not_called()


if __name__ == "__main__": unittest.main()
