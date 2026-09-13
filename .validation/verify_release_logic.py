#!/usr/bin/env python3
"""Exercise the original release shell steps without making GitHub API calls.

The input JSON contains the parsed, complete stockfish and official_release
workflows. Only fixed GitHub expression values are substituted into their run
scripts. Git and Bash are real; gh is a recording stub for existence guards.
"""

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
FIXTURE_DATE = "2026-09-13T12:00:00+0000"


def get_script(sources, workflow, job, step_name):
    matches = [
        step["run"]
        for step in sources[workflow]["jobs"][job]["steps"]
        if step.get("name") == step_name and "run" in step
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one run step: {workflow}/{job}/{step_name}")
    return matches[0]


def substitute(script, context):
    def replace(match):
        expression = match.group(1).strip()
        if expression not in context:
            raise ValueError(f"Unresolved GitHub expression: {expression}")
        return context[expression]

    return EXPRESSION.sub(replace, script)


def environment():
    # Do not carry tokens or other GitHub secrets into local script tests.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": FIXTURE_DATE,
        "GIT_COMMITTER_DATE": FIXTURE_DATE,
    }


def git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=repo, env=environment(), text=True,
        capture_output=True, timeout=30, check=True,
    )
    return result.stdout.strip()


def commit(repo, message):
    git(
        repo, "-c", "user.name=Release validation fixture",
        "-c", "user.email=release-validation@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message,
    )


def key_values(path):
    values = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise AssertionError(f"Invalid or duplicate output entry: {line!r}")
        values[key] = value
    return values


def run_step(script, repo, scratch, context=None, variables=None, gh_status=None):
    scratch.mkdir()
    output_path = scratch / "github_output"
    env_path = scratch / "github_env"
    output_path.touch()
    env_path.touch()
    env = environment()
    env.update({"GITHUB_OUTPUT": str(output_path), "GITHUB_ENV": str(env_path)})
    env.update(variables or {})
    if gh_status is not None:
        bin_path = scratch / "bin"
        bin_path.mkdir()
        gh_path = bin_path / "gh"
        gh_path.write_text(
            '#!/bin/sh\nprintf \'%s\\0\' "$@" >> "$GH_CALL_LOG"\nexit "$GH_STATUS"\n'
        )
        gh_path.chmod(0o755)
        env.update({
            "PATH": str(bin_path) + os.pathsep + env["PATH"],
            "GH_CALL_LOG": str(scratch / "gh_calls"),
            "GH_STATUS": str(gh_status),
        })
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         substitute(script, context or {})],
        cwd=repo, env=env, text=True, capture_output=True, timeout=30,
    )
    call_log = scratch / "gh_calls"
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "outputs": key_values(output_path),
        "environment": key_values(env_path),
        "gh_arguments": call_log.read_bytes().decode().split("\0")[:-1]
        if call_log.exists() else [],
    }


def record(cases, name, actual, *, exit_code=0, stdout="", outputs=None,
           exported=None, gh_arguments=None, extra_checks=None):
    expected = {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": "",
        "outputs": outputs or {},
        "environment": exported or {},
        "gh_arguments": gh_arguments or [],
    }
    checks = {key: actual[key] == value for key, value in expected.items()}
    checks.update(extra_checks or {})
    cases.append({
        "name": name, "passed": all(checks.values()),
        "checks": checks, "expected": expected, "actual": actual,
    })


def verify(sources):
    scripts = {
        "official_detection": get_script(sources, "stockfish", "Prerelease", "Official Release?"),
        "prerelease_metadata": get_script(
            sources, "stockfish", "Prerelease", "Compute prerelease metadata"
        ),
        "validate_request": get_script(
            sources, "official_release", "OfficialReleaseDraft", "Validate release request"
        ),
        "release_exists": get_script(
            sources, "official_release", "OfficialReleaseDraft", "Ensure tag does not already exist"
        ),
        "git_tag_exists": get_script(
            sources, "official_release", "OfficialReleaseDraft", "Ensure git tag does not already exist"
        ),
    }
    cases = []
    with tempfile.TemporaryDirectory(prefix="stockfish-release-logic-") as directory:
        root = Path(directory)
        repo = root / "repository"
        repo.mkdir()
        git(repo, "init", "-q")

        def execute(script_name, **kwargs):
            return run_step(
                scripts[script_name], repo, root / f"case-{len(cases):03d}", **kwargs
            )

        for name, message, official in [
            ("ordinary", "A normal development change", False),
            ("official_subject", "Official release version of Stockfish 99", True),
            ("official_body", "Release preparation\n\nOfficial release version of Stockfish 99", True),
            ("similar_nonmatching", "Official release candidate of Stockfish 99", False),
        ]:
            commit(repo, message)
            record(
                cases, f"official_detection/{name}", execute("official_detection"),
                exported={"OFFICIAL_RELEASE": str(official).lower()},
            )

        fixture_sha = git(repo, "rev-parse", "HEAD")[:8]
        for branch, changes, official in itertools.product(
            ["master", "feature-validation"], ["0", "1", "12"], ["false", "true"]
        ):
            eligible = branch == "master" and changes == "0" and official == "false"
            record(
                cases, f"prerelease_metadata/{branch}/changes-{changes}/official-{official}",
                execute(
                    "prerelease_metadata", context={"github.ref_name": branch},
                    variables={"CHANGES": changes, "OFFICIAL_RELEASE": official},
                ),
                outputs={
                    "release_name": f"Stockfish dev-20260913-{fixture_sha}" if eligible else "",
                    "release_tag": f"stockfish-dev-20260913-{fixture_sha}" if eligible else "",
                },
            )

        validation_cases = [
            ("major", "master", "sf_99", "Stockfish 99", ""),
            ("minor", "master", "sf_99.1", "Stockfish 99.1", ""),
            ("wrong_branch", "feature-validation", "sf_99", "Stockfish 99",
             "Official releases must be dispatched from master.\n"),
            ("empty_title", "master", "sf_99", "", "release_title is required.\n"),
        ]
        invalid_tags = {
            "empty": "", "prefix_only": "sf_", "missing_prefix": "99",
            "different_prefix": "v99", "capitalized_prefix": "SF_99",
            "negative_version": "sf_-1", "missing_major": "sf_.1",
            "missing_minor": "sf_99.", "patch_version": "sf_99.1.2",
            "suffix": "sf_99rc1", "leading_space": " sf_99",
            "trailing_space": "sf_99 ", "newline": "sf_99\n",
            "semicolon": "sf_99; touch validation-injection-marker",
            "command_substitution": "$(touch validation-injection-marker)",
            "backticks": "`touch validation-injection-marker`",
            "quotes": 'sf_99"; touch validation-injection-marker; "',
            "glob": "sf_*", "slash": "sf_99/1",
        }
        validation_cases.extend(
            (name, "master", tag, "Stockfish 99", "release_tag must match sf_X or sf_X.Y\n")
            for name, tag in invalid_tags.items()
        )
        for name, branch, tag, title, expected_stdout in validation_cases:
            actual = execute(
                "validate_request", context={"github.ref_name": branch},
                variables={"RELEASE_TAG": tag, "RELEASE_TITLE": title},
            )
            record(
                cases, f"validate_request/{name}", actual,
                exit_code=1 if expected_stdout else 0, stdout=expected_stdout,
                extra_checks={"no_injected_command_executed": not (
                    repo / "validation-injection-marker"
                ).exists()},
            )

        fixed_context = {
            "inputs.release_tag": "sf_99",
            "github.repository": "official-stockfish/Stockfish",
        }
        for script_name, arguments, rejection in [
            ("release_exists", ["release", "view", "sf_99"],
             "Release already exists for this tag.\n"),
            ("git_tag_exists", ["api", "repos/official-stockfish/Stockfish/git/ref/tags/sf_99"],
             "Tag already exists.\n"),
        ]:
            for status, name in [(0, "exists"), (1, "not_found"), (2, "other_error_current_behavior")]:
                record(
                    cases, f"{script_name}/{name}",
                    execute(script_name, context=fixed_context, gh_status=status),
                    exit_code=1 if status == 0 else 0,
                    stdout=rejection if status == 0 else "", gh_arguments=arguments,
                )

    return {
        "passed": all(case["passed"] for case in cases),
        "total_cases": len(cases),
        "passed_cases": sum(case["passed"] for case in cases),
        "failed_cases": [case["name"] for case in cases if not case["passed"]],
        "scope": (
            "Original workflow run scripts executed with real Bash and local Git. "
            "GitHub context expressions use fixed fixture values; gh is a recording stub. "
            "This verifies shell branches, exit codes, outputs and arguments, "
            "not GitHub API behavior or token permissions."
        ),
        "preexisting_limitations": [
            "Both existence guards treat any nonzero gh exit as absence, including other errors. "
            "The status-2 cases document the original behavior; they do not endorse it."
        ],
        "source_script_sha256": {
            name: hashlib.sha256(script.encode()).hexdigest()
            for name, script in scripts.items()
        },
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        for executable in ("bash", "git"):
            if not shutil.which(executable):
                raise RuntimeError(f"Required executable not found: {executable}")
        report = verify(json.loads(args.source_json.read_text()))
    except (OSError, ValueError, KeyError, AssertionError, subprocess.SubprocessError) as error:
        report = {"passed": False, "error": str(error), "error_type": type(error).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: report[key] for key in (
        "passed", "total_cases", "passed_cases", "failed_cases", "error"
    ) if key in report}, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
