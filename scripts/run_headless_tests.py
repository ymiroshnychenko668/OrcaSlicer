#!/usr/bin/env python3
"""Run the existing Linux CLI RequiresApp CTests with display variables removed.

Requires a configured app build with all six tests/cli tests and a built OrcaSlicer.
The output directory must be new. HOME, XDG directories and temporary files are
isolated there; DISPLAY and WAYLAND_DISPLAY are removed. CTest runs serially using
the tests' own timeouts. Missing, disabled, skipped and unreported tests fail the
runner even when CTest returns zero. Explicit SKIP messages inside a passing test
also mean incomplete coverage, not a headless-runtime failure.
Removing variables does not prove that local display sockets are inaccessible;
an isolated container without those sockets is a separate execution gate.

The manifest records discovery, commands, binary hashes, environment assumptions,
CTest return codes and checked JUnit results. This is a test runner, not a build
or an assertion that a particular Orca revision already works headlessly.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


REQUIRED_TESTS = {
    "cli_strict_mode": "test_cli_strict.sh",
    "cli_empty_project_config": "test_cli_empty_project_config.sh",
    "cli_project_missing_keys": "test_cli_project_missing_keys.sh",
    "cli_arrange_rotations": "test_cli_arrange_rotations.sh",
    "cli_no_suitable_objects": "test_cli_no_suitable_objects.sh",
    "cli_malformed_input": "test_cli_malformed_input.sh",
}
DIRECTORIES = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "xdg/config",
    "XDG_CACHE_HOME": "xdg/cache",
    "XDG_DATA_HOME": "xdg/data",
    "XDG_STATE_HOME": "xdg/state",
    "XDG_RUNTIME_DIR": "xdg/runtime",
    "XDG_CONFIG_DIRS": "xdg/config-dirs",
    "XDG_DATA_DIRS": "xdg/data-dirs",
    "TMPDIR": "tmp",
    "TMP": "tmp",
    "TEMP": "tmp",
}
DISPLAY_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")
SKIP_MESSAGE = re.compile(r"^\s*SKIP(?:\b|:)[^\n]*", re.MULTILINE)


class RunError(Exception):
    def __init__(self, message, kind="incomplete_coverage"):
        super().__init__(message)
        self.kind = kind


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def isolated_environment(output, original):
    env = original.copy()
    for key in DISPLAY_VARIABLES:
        env.pop(key, None)
    for key, relative in DIRECTORIES.items():
        directory = output / relative
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(directory)
    return env


def profile_catalog(build):
    profiles = build / "resources/profiles"
    result = {"path": str(profiles), "resolved_path": str(profiles.resolve()), "exists": profiles.is_dir()}
    if profiles.is_dir():
        digest = hashlib.sha256()
        files = sorted(path for path in profiles.rglob("*") if path.is_file())
        for path in files:
            digest.update((path.relative_to(profiles).as_posix() + "\0" + sha256(path) + "\n").encode())
        result.update(file_count=len(files), sha256=digest.hexdigest(),
                      hash_format="SHA256(sorted relative-path + NUL + file-SHA256 + newline)")
    return result


def validate_discovery(document, source):
    tests = document.get("tests", [])
    names = [test.get("name") for test in tests]
    missing = sorted(set(REQUIRED_TESTS) - set(names))
    if missing:
        raise RunError("Required CLI tests were not discovered: " + ", ".join(missing))
    if len(names) != len(set(names)):
        raise RunError("CTest discovery contains duplicate test names")
    for test in tests:
        name, command = test["name"], test.get("command", [])
        properties = {item["name"]: item["value"] for item in test.get("properties", [])}
        if "RequiresApp" not in properties.get("LABELS", []):
            raise RunError(name + ": missing RequiresApp label")
        if properties.get("DISABLED"):
            raise RunError(name + ": disabled test")
        if len(command) < 4 or Path(command[0]).name != "bash":
            raise RunError(name + ": expected bash script, OrcaSlicer binary and Python command")
        script = Path(command[1]).resolve()
        if not script.is_file() or script.parent != (source / "tests/cli").resolve():
            raise RunError(name + ": command does not use an existing tests/cli script")
        if name in REQUIRED_TESTS and script.name != REQUIRED_TESTS[name]:
            raise RunError(name + ": command uses the wrong CLI test script")
        binary = Path(command[2])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise RunError(name + ": OrcaSlicer executable is missing or not executable: " + str(binary))
        # CTest properties must not silently undo the environment isolation.
        for prop in ("ENVIRONMENT", "ENVIRONMENT_MODIFICATION"):
            for assignment in properties.get(prop, []):
                if assignment.split("=", 1)[0] in set(DIRECTORIES) | set(DISPLAY_VARIABLES):
                    raise RunError(name + ": " + prop + " overrides isolated environment")
    if len({test["command"][2] for test in tests}) != 1:
        raise RunError("Discovered CLI tests do not use the same OrcaSlicer executable")
    return tests


def inspect_junit(path, expected_names):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise RunError("Missing or invalid CTest JUnit: " + str(error)) from error
    if root.tag not in ("testsuite", "testsuites"):
        raise RunError("Unexpected JUnit root: " + root.tag)
    cases = list(root.iter("testcase"))
    names = [case.get("name") for case in cases]
    results = [{"name": case.get("name"), "status": case.get("status"),
                "failures": [item.attrib for item in case.findall("failure")],
                "errors": [item.attrib for item in case.findall("error")],
                "skipped": [item.attrib for item in case.findall("skipped")],
                "skip_messages": SKIP_MESSAGE.findall("\n".join(case.itertext()))}
               for case in cases]
    counts = {"tests": len(cases), "failures": sum(bool(r["failures"]) for r in results),
              "errors": sum(bool(r["errors"]) for r in results),
              "skipped": sum(bool(r["skipped"]) for r in results),
              "disabled": sum(r["status"] == "disabled" for r in results)}
    problems = []
    if not names or Counter(names) != Counter(expected_names):
        problems.append("JUnit test names/count do not match discovery")
    # CTest 3.28 emits tests/failures/skipped/disabled. Verify reported counts
    # against testcase elements, including all descendant suites if wrapped.
    for suite in (item for item in root.iter() if item.tag in ("testsuite", "testsuites")):
        children = list(suite.iter("testcase"))
        actual = {"tests": len(children), "failures": sum(c.find("failure") is not None for c in children),
                  "errors": sum(c.find("error") is not None for c in children),
                  "skipped": sum(c.find("skipped") is not None for c in children),
                  "disabled": sum(c.get("status") == "disabled" for c in children)}
        for key, value in actual.items():
            declared = suite.get(key)
            if declared is not None and declared != str(value):
                problems.append("JUnit " + key + " count disagrees with testcase results")
        if suite.tag == "testsuite" and suite.get("tests") is None:
            problems.append("JUnit suite has no declared test count")
    for result in results:
        if result["skipped"] or result["status"] in ("notrun", "disabled") or result["skip_messages"]:
            problems.append(str(result["name"]) + ": incomplete coverage (skipped/disabled or explicit SKIP message)")
        elif result["status"] != "run" or result["failures"] or result["errors"]:
            problems.append(str(result["name"]) + ": test did not pass")
    return {"counts": counts, "testcases": results, "problems": problems}


def run(args):
    if not sys.platform.startswith("linux"):
        raise RunError("This runner supports Linux only", "preflight_failure")
    if os.path.lexists(args.output_dir):
        raise RunError("Output directory already exists; use a new directory: " + str(args.output_dir),
                       "preflight_failure")
    output = args.output_dir.resolve()
    try:
        output.mkdir(parents=True, mode=0o700)
    except FileExistsError as error:
        raise RunError("Output directory already exists; use a new directory: " + str(output),
                       "preflight_failure") from error
    source = Path(__file__).resolve().parent.parent
    build = args.build_dir.resolve()
    test_dir = build / "tests"
    original = os.environ.copy()
    env = isolated_environment(output, original)
    manifest = {
        "format_version": 1, "status": "running", "runner_return_code": None,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source), "build_dir": str(build), "test_dir": str(test_dir),
        "config": args.config, "required_tests": sorted(REQUIRED_TESTS), "commands": [],
        "executables": [], "environment": {
            "original_directory_values": {key: original.get(key) for key in DIRECTORIES},
            "original_display_values": {key: original.get(key) for key in DISPLAY_VARIABLES},
            "removed_variables": list(DISPLAY_VARIABLES),
            "isolated_directory_values": {key: env[key] for key in DIRECTORIES},
            "inherited_runtime_environment": {key: original.get(key) for key in
                                              ("PATH", "LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")},
            "assumptions": "Other environment variables and system/runtime libraries are inherited; no Xvfb is started.",
        },
    }

    def save_manifest():
        pending = output / "manifest.json.tmp"
        pending.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        pending.replace(output / "manifest.json")

    def execute(argv, prefix):
        record = {"argv": argv, "cwd": str(output), "return_code": None,
                  "stdout": prefix + ".stdout.log", "stderr": prefix + ".stderr.log"}
        manifest["commands"].append(record)
        save_manifest()
        with (output / record["stdout"]).open("wb") as stdout, (output / record["stderr"]).open("wb") as stderr:
            completed = subprocess.run(argv, cwd=output, env=env, stdout=stdout, stderr=stderr, check=False)
        record["return_code"] = completed.returncode
        save_manifest()
        return completed.returncode

    save_manifest()
    code = 1
    try:
        manifest["source_snapshot"] = {"runner_sha256": sha256(Path(__file__).resolve())}
        for key, arguments in (("head", ["rev-parse", "HEAD"]),
                               ("tracked_status", ["status", "--porcelain", "--untracked-files=no"])):
            result = subprocess.run(["git", "-C", str(source), *arguments], env=env,
                                    capture_output=True, text=True, check=False)
            manifest["source_snapshot"][key] = {"return_code": result.returncode, "stdout": result.stdout.strip(),
                                                 "stderr": result.stderr.strip()}
        manifest["profile_catalog_before"] = profile_catalog(build)
        ctest = shutil.which("ctest", path=env.get("PATH"))
        if not ctest or not (test_dir / "CTestTestfile.cmake").is_file():
            raise RunError("ctest and a configured <build-dir>/tests/CTestTestfile.cmake are required",
                           "preflight_failure")
        base = [ctest, "--test-dir", str(test_dir), "-C", args.config, "-L", "RequiresApp"]
        if execute(base + ["--show-only=json-v1"], "discovery"):
            raise RunError("CTest discovery failed", "preflight_failure")
        discovery = json.loads((output / "discovery.stdout.log").read_text(encoding="utf-8"))
        manifest["discovered_tests"] = discovery.get("tests", [])
        # Preserve available binary fingerprints even when another test fails preflight.
        binaries = {test["command"][2] for test in discovery.get("tests", []) if len(test.get("command", [])) > 2}
        for binary in sorted(binaries):
            path = Path(binary)
            manifest["executables"].append({"path": binary, "exists": path.is_file(),
                                             "sha256_before": sha256(path) if path.is_file() else None})
        tests = validate_discovery(discovery, source)
        junit = output / "ctest.junit.xml"
        ctest_code = execute(base + ["--no-tests=error", "--output-junit", str(junit),
                                     "--output-on-failure", "-j1"], "ctest")
        report = inspect_junit(junit, [test["name"] for test in tests])
        manifest["junit"] = report
        manifest["profile_catalog_after"] = profile_catalog(build)
        if manifest["profile_catalog_after"] != manifest["profile_catalog_before"]:
            report["problems"].append("Profile catalog changed during the test run")
        for binary in manifest["executables"]:
            path = Path(binary["path"])
            binary["sha256_after"] = sha256(path) if path.is_file() else None
            if binary["sha256_after"] != binary["sha256_before"]:
                report["problems"].append("OrcaSlicer executable changed during the test run")
        if ctest_code or report["problems"]:
            incomplete = any("incomplete coverage" in problem or "match discovery" in problem or "count disagrees" in problem
                             for problem in report["problems"])
            raise RunError("CTest returned " + str(ctest_code) + "; " + "; ".join(report["problems"]),
                           "incomplete_coverage" if incomplete else "test_failure")
        code = 0
        manifest["status"] = "passed"
    except (RunError, OSError, ValueError, TypeError, KeyError) as error:
        manifest.update(status="failed", failure_kind=getattr(error, "kind", "runner_failure"), error=str(error))
        print("Headless tests failed: " + str(error), file=sys.stderr)
    except KeyboardInterrupt:
        code = 130
        manifest.update(status="interrupted", failure_kind="interrupted")
    finally:
        manifest["runner_return_code"] = code
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save_manifest()
    print("Headless test evidence: " + str(output / "manifest.json"))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", type=Path, required=True, help="Configured app CMake build; tests are read from <dir>/tests")
    parser.add_argument("--output-dir", type=Path, required=True, help="New evidence directory; an existing path is rejected")
    parser.add_argument("--config", default="Release", help="CMake configuration (default: Release)")
    args = parser.parse_args()
    try:
        return run(args)
    except (RunError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
