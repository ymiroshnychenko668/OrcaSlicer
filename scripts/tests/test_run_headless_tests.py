"""Runner guard tests with synthetic CTest/JUnit data; no native validation.

Run: python3 -m unittest discover -s scripts/tests -p test_run_headless_tests.py -v
"""

import argparse
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_headless_tests as runner


class HeadlessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "source"
        (self.source / "tests/cli").mkdir(parents=True)
        self.binary = self.directory / "orca-slicer"
        self.binary.write_text("synthetic fixture; never executed")
        self.binary.chmod(0o700)
        self.discovery = {"tests": []}
        for name, script in runner.REQUIRED_TESTS.items():
            path = self.source / "tests/cli" / script
            path.write_text("synthetic fixture; never executed")
            self.discovery["tests"].append({"name": name, "command": ["/bin/bash", str(path), str(self.binary), sys.executable],
                                            "properties": [{"name": "LABELS", "value": ["CLI", "RequiresApp"]}]})

    def junit(self):
        root = ET.Element("testsuite", tests="6", failures="0", skipped="0", disabled="0")
        for name in runner.REQUIRED_TESTS:
            ET.SubElement(root, "testcase", name=name, status="run")
        return root

    def inspect(self, root):
        path = self.directory / "junit.xml"
        ET.ElementTree(root).write(path)
        return runner.inspect_junit(path, list(runner.REQUIRED_TESTS))

    def test_current_complete_discovery_is_accepted(self):
        self.assertEqual(len(runner.validate_discovery(self.discovery, self.source)), 6)
        self.assertFalse(self.inspect(self.junit())["problems"])

    def test_zero_and_missing_discovery_fail_before_execution(self):
        for count in (0, 5):
            with self.subTest(count=count), self.assertRaisesRegex(runner.RunError, "not discovered"):
                runner.validate_discovery({"tests": self.discovery["tests"][:count]}, self.source)

    def test_missing_app_binary_fails_before_skip_return_code_can_hide_it(self):
        self.binary.unlink()
        with self.assertRaisesRegex(runner.RunError, "executable is missing"):
            runner.validate_discovery(self.discovery, self.source)

    def test_disabled_discovery_fails(self):
        self.discovery["tests"][0]["properties"].append({"name": "DISABLED", "value": True})
        with self.assertRaisesRegex(runner.RunError, "disabled test"):
            runner.validate_discovery(self.discovery, self.source)

    def test_ctest_cannot_restore_a_display(self):
        self.discovery["tests"][0]["properties"].append({"name": "ENVIRONMENT", "value": ["DISPLAY=:0"]})
        with self.assertRaisesRegex(runner.RunError, "overrides isolated"):
            runner.validate_discovery(self.discovery, self.source)

    def test_skipped_disabled_and_subcase_skip_are_not_green(self):
        for status in ("skipped", "disabled", "subcase"):
            with self.subTest(status=status):
                root = self.junit()
                case = root.find("testcase")
                if status == "subcase":
                    ET.SubElement(case, "system-out").text = "SKIP: the baseline equals the default\n"
                elif status == "disabled":
                    case.set("status", "disabled")
                    root.set("disabled", "1")
                else:
                    case.set("status", "notrun")
                    ET.SubElement(case, "skipped", message="SKIP_RETURN_CODE=77")
                    root.set("skipped", "1")
                self.assertTrue(any("incomplete coverage" in p for p in self.inspect(root)["problems"]))

    def test_zero_missing_duplicate_or_failed_junit_is_not_green(self):
        roots = [ET.Element("testsuite", tests="0"), self.junit(), self.junit(), self.junit()]
        roots[1].remove(roots[1].find("testcase"))
        roots[1].set("tests", "5")
        roots[2].append(copy.deepcopy(roots[2].find("testcase")))
        roots[2].set("tests", "7")
        roots[3].find("testcase").set("status", "fail")
        ET.SubElement(roots[3].find("testcase"), "failure", message="exit 1")
        roots[3].set("failures", "1")
        for index, root in enumerate(roots):
            with self.subTest(index=index):
                self.assertTrue(self.inspect(root)["problems"])

    def test_declared_junit_counts_must_match_actual_cases(self):
        root = self.junit()
        root.set("tests", "7")
        self.assertTrue(any("count disagrees" in p for p in self.inspect(root)["problems"]))

    def test_profile_fingerprint_tracks_actual_symlink_target_and_content(self):
        profiles = self.source / "resources/profiles"
        profiles.mkdir(parents=True)
        preset = profiles / "test.json"
        preset.write_text('{}')
        build = self.directory / "build"
        build.mkdir()
        (build / "resources").symlink_to(self.source / "resources", target_is_directory=True)
        before = runner.profile_catalog(build)
        self.assertEqual(before["resolved_path"], str(profiles))
        self.assertEqual(before["file_count"], 1)
        preset.write_text('{"changed": true}')
        self.assertNotEqual(before["sha256"], runner.profile_catalog(build)["sha256"])

    def test_environment_is_isolated_without_changing_callers_environment(self):
        original = {"HOME": "/old", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "PATH": "/bin"}
        env = runner.isolated_environment(self.directory / "isolation", original)
        self.assertEqual(original["DISPLAY"], ":0")
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)
        for key in runner.DIRECTORIES:
            self.assertTrue(Path(env[key]).is_dir())
            self.assertTrue(Path(env[key]).is_relative_to(self.directory))
        self.assertEqual(os.stat(env["XDG_RUNTIME_DIR"]).st_mode & 0o777, 0o700)

    def test_existing_output_is_rejected_without_reusing_stale_evidence(self):
        stale = self.directory / "manifest.json"
        stale.write_text("stale fixture")
        with self.assertRaisesRegex(runner.RunError, "already exists"):
            runner.run(argparse.Namespace(output_dir=self.directory, build_dir=self.directory, config="Release"))
        self.assertEqual(stale.read_text(), "stale fixture")


if __name__ == "__main__":
    unittest.main()
