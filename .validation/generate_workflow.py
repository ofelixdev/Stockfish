#!/usr/bin/env python3
"""Generate fork-only runner comparisons from the actual candidate workflows."""

import argparse
import copy
import json
from pathlib import Path
import re
import shutil

import yaml


HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=HERE.parent if HERE.name == '.validation' else HERE.parent / 'stockfish')
parser.add_argument('--target', type=Path, default=HERE.parent if HERE.name == '.validation' else HERE.parent / 'stockfish-validation-checkout')
parser.add_argument('--helpers', type=Path, default=HERE)
args = parser.parse_args()
ROOT, SOURCE, TARGET = args.helpers.resolve(), args.source.resolve(), args.target.resolve()
CHECKOUT = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
RUN_ID = "${{ github.run_id }}"
REPO = "ofelixdev/Stockfish"


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = copy.deepcopy(Loader.yaml_implicit_resolvers)
for initial, resolvers in Loader.yaml_implicit_resolvers.items():
    Loader.yaml_implicit_resolvers[initial] = [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)


class Dumper(yaml.SafeDumper):
    pass


def represent_string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


Dumper.add_representer(str, represent_string)


def replace(value, mapping):
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: replace(item, mapping) for key, item in value.items()}
    return value


def step(name, script, **kwargs):
    return {"name": name, "run": script, **kwargs}


sources = {}
for name in ("stockfish", "official_release", "upload_binaries"):
    sources[name] = yaml.load((SOURCE / f".github/workflows/{name}.yml").read_text(), Loader=Loader)
validation = TARGET / ".validation"
validation.mkdir(exist_ok=True)
(validation / "source.json").write_text(json.dumps(sources, indent=2) + "\n")
for name in ("verify_package.py", "test_verify_package.py", "verify_release_logic.py", "verify_live_results.py"):
    if (ROOT / name).resolve() != (validation / name).resolve():
        shutil.copy2(ROOT / name, validation / name)
if Path(__file__).resolve() != (validation / 'generate_workflow.py').resolve():
    shutil.copy2(__file__, validation / "generate_workflow.py")

jobs = {}
for label, runner in (("Latest", "ubuntu-latest"), ("Slim", "ubuntu-slim")):
    for prefix, source_name in (("Matrix", "stockfish"), ("OfficialMatrix", "official_release")):
        job = copy.deepcopy(sources[source_name]["jobs"]["Matrix"])
        job["name"] = f"{source_name} matrices on {runner}"
        job["runs-on"] = runner
        job["steps"].append(step(
            "Verify actual matrix outputs",
            "python3 - <<'PY'\n"
            "import json, os, pathlib\n"
            "for kind in ('arm', 'universal'):\n"
            "    expected = json.loads(pathlib.Path(f'.github/ci/{kind}_matrix.json').read_text())\n"
            "    actual = json.loads(os.environ[kind.upper() + '_ACTUAL'])\n"
            "    assert actual == expected, kind\n"
            "print('Both matrix outputs match the checked-in inputs')\n"
            "PY\n",
            env={"ARM_ACTUAL": "${{ steps.set-arm-matrix.outputs.arm_matrix }}",
                 "UNIVERSAL_ACTUAL": "${{ steps.set-universal-matrix.outputs.universal_matrix }}"},
        ))
        jobs[prefix + label] = job
    jobs["Logic" + label] = {
        "name": f"Release decision cases on {runner}", "runs-on": runner,
        "steps": [
            {"uses": CHECKOUT, "with": {"persist-credentials": False}},
            step("Test the archive verifier", "python3 -m unittest discover -s .validation -p 'test_verify_package.py' -v"),
            step("Exercise the original release scripts", "python3 .validation/verify_release_logic.py --source-json .validation/source.json --output release-logic.json"),
            {"uses": UPLOAD, "with": {"name": f"release-logic-{label.lower()}", "path": "release-logic.json", "retention-days": 1}},
        ],
    }

    dev = copy.deepcopy(sources["stockfish"]["jobs"]["Prerelease"])
    dev["name"] = f"Create development draft on {runner}"
    dev["runs-on"] = runner
    dev["if"] = f"github.repository == '{REPO}'"
    dev["needs"] = ["Matrix" + label] + (["DevLatest"] if label == "Slim" else [])
    dev = replace(dev, {
        "${{ github.ref_name }}": "master",
        "steps.prerelease_metadata.outputs.release_tag": "steps.scoped_tag.outputs.release_tag",
    })
    dev["env"] = {
        "PREVIOUS_TAG": f"validation-previous-{RUN_ID}-{label.lower()}",
        "STABLE_TAG": f"validation-stable-{RUN_ID}-{label.lower()}",
    }
    revised_steps = []
    for original_step in dev["steps"]:
        name = original_step.get("name", "")
        if name == "Get Latest Dev Prerelease Tag":
            empty_probe = copy.deepcopy(original_step)
            empty_probe["name"] = "Exercise no previous prerelease"
            revised_steps.extend([
                empty_probe,
                step("Verify empty prerelease lookup", 'test "$COMMIT_SHA_TAG" = null'),
                step("Create known previous and stable release fixtures", '''set -euo pipefail
gh release create "$PREVIOUS_TAG" --repo "$GITHUB_REPOSITORY" --target "$GITHUB_SHA" --prerelease --title "Temporary runner validation" --notes "Temporary fixture for Stockfish issue 7080."
gh release create "$STABLE_TAG" --repo "$GITHUB_REPOSITORY" --target "$GITHUB_SHA" --title "Temporary stable fixture" --notes "Temporary fixture; removed by this workflow."
''', env={"GH_TOKEN": "${{ github.token }}"}),
            ])
        revised_steps.append(original_step)
        if name == "Compute prerelease metadata":
            revised_steps.append(step(
                "Scope the fixture tag to this run and runner",
                f'test -n "$SOURCE_TAG"\necho "release_tag=$SOURCE_TAG-{RUN_ID}-{label.lower()}" >> "$GITHUB_OUTPUT"',
                id="scoped_tag",
                env={"SOURCE_TAG": "${{ steps.prerelease_metadata.outputs.release_tag }}"},
            ))
        if name == "Get Latest Dev Prerelease Tag":
            revised_steps.append(step("Verify selection ignores stable releases", 'test "$COMMIT_SHA_TAG" = "$PREVIOUS_TAG"'))
        if name == "Delete Previous Dev Prerelease":
            revised_steps.append(step("Verify previous release and tag were deleted", '''set -euo pipefail
if gh release view "$PREVIOUS_TAG" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then exit 1; fi
if gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$PREVIOUS_TAG" >/dev/null 2>&1; then exit 1; fi
''', env={"GH_TOKEN": "${{ github.token }}"}))
    dev["steps"] = revised_steps
    jobs["Dev" + label] = dev

    official = copy.deepcopy(sources["official_release"]["jobs"]["OfficialReleaseDraft"])
    official["name"] = f"Create official draft on {runner}"
    official["runs-on"] = runner
    official["if"] = f"github.repository == '{REPO}'"
    official["needs"] = ["OfficialMatrix" + label]
    number = "1" if label == "Latest" else "2"
    official = replace(official, {
        "${{ github.ref_name }}": "master",
        "${{ inputs.release_tag }}": f"sf_7080.{RUN_ID}{number}",
        "${{ inputs.release_title }}": f"Temporary official draft on {runner}",
    })
    jobs["Official" + label] = official

universal = json.loads((SOURCE / ".github/ci/universal_matrix.json").read_text())["include"]
arm = json.loads((SOURCE / ".github/ci/arm_matrix.json").read_text())
entries = list(universal)
for config in arm["config"]:
    for binary in arm["binaries"]:
        if not any(excluded["binaries"] == binary and all(config.get(k) == v for k, v in excluded["config"].items()) for excluded in arm["exclude"]):
            entries.append({"config": config, "binaries": binary})
assert len(entries) == 8
matrix = []
for label, runner in (("latest", "ubuntu-latest"), ("slim", "ubuntu-slim")):
    for entry in entries:
        matrix.append({**entry, "label": label, "runner": runner,
                       "case_id": entry["config"]["simple_name"] + "-" + entry["binaries"]})

package = copy.deepcopy(sources["upload_binaries"]["jobs"]["Artifacts"])
package["name"] = "Package ${{ matrix.case_id }} on ${{ matrix.runner }}"
package["runs-on"] = "${{ matrix.runner }}"
package["needs"] = ["DevLatest", "DevSlim", "OfficialLatest", "OfficialSlim"]
package["permissions"] = {"contents": "write", "actions": "read"}
package["strategy"]["matrix"] = {"include": matrix}
package["env"].update({
    "DEV_TAG": "${{ matrix.label == 'latest' && needs.DevLatest.outputs.release_tag || needs.DevSlim.outputs.release_tag }}",
    "OFFICIAL_TAG": "${{ matrix.label == 'latest' && needs.OfficialLatest.outputs.release_tag || needs.OfficialSlim.outputs.release_tag }}",
    "CASE_ID": "${{ matrix.case_id }}", "LABEL": "${{ matrix.label }}",
    "ARCHIVE_EXT": "${{ matrix.config.archive_ext }}",
})
new_steps = []
for original_step in package["steps"]:
    if original_step.get("name") == "Download artifact from compilation":
        original_step["with"].update({"repository": "official-stockfish/Stockfish", "run-id": 34745589340, "github-token": "${{ github.token }}"})
    if original_step.get("name") == "Upload Draft Release Asset":
        new_steps.append(step("Verify every packaged file and record archive digest", '''set -euo pipefail
ARCHIVE="stockfish-$NAME-$BINARY.$ARCHIVE_EXT"
python3 .validation/verify_package.py verify --fixture stockfish-workflow --wiki stockfish/wiki --archive "$ARCHIVE" --binary-name "stockfish-$NAME-$BINARY$EXT" --manifest "validation-results/$LABEL-$CASE_ID.json"
python3 - <<'PY'
import hashlib, json, os, pathlib
name = f"stockfish-{os.environ['NAME']}-{os.environ['BINARY']}.{os.environ['ARCHIVE_EXT']}"
path = pathlib.Path(name)
with path.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
result = {'archive': name, 'sha256': digest, 'size': path.stat().st_size, 'case_id': os.environ['CASE_ID'], 'label': os.environ['LABEL']}
pathlib.Path(f"validation-results/{result['label']}-{result['case_id']}.meta.json").write_text(json.dumps(result, indent=2) + '\\n')
PY
'''))
        for prerelease, tag in ((True, "DEV_TAG"), (False, "OFFICIAL_TAG")):
            upload = copy.deepcopy(original_step)
            upload["name"] = "Upload development draft asset" if prerelease else "Upload official draft asset"
            upload["if"] = f"env.{tag} != ''"
            upload["with"].update({"tag_name": "${{ env." + tag + " }}", "prerelease": prerelease, "token": "${{ github.token }}"})
            new_steps.append(upload)
        new_steps.append({"uses": UPLOAD, "with": {"name": "manifest-${{ matrix.label }}-${{ matrix.case_id }}", "path": "validation-results/*", "if-no-files-found": "error", "retention-days": 1}})
    else:
        new_steps.append(original_step)
package["steps"] = new_steps
jobs["Packages"] = package

for label, runner in (("Latest", "ubuntu-latest"), ("Slim", "ubuntu-slim")):
    publish = copy.deepcopy(sources["stockfish"]["jobs"]["PublishPrerelease"])
    publish["name"] = f"Publish the verified development draft on {runner}"
    publish["runs-on"] = runner
    publish["needs"] = ["Dev" + label, "Packages"]
    publish = replace(publish, {"official-stockfish/Stockfish": REPO, "needs.Prerelease": "needs.Dev" + label})
    jobs["Publish" + label] = publish

all_previous = list(jobs)
jobs["Verify"] = {
    "name": "Compare all payloads and download all release assets", "runs-on": "ubuntu-latest",
    # GitHub requires push access to inspect unpublished draft releases.
    "needs": all_previous, "if": "always()", "permissions": {"contents": "write", "actions": "read"},
    "env": {"GH_TOKEN": "${{ github.token }}", "NEEDS_JSON": "${{ toJson(needs) }}"},
    "steps": [
        {"uses": CHECKOUT, "with": {"persist-credentials": False}},
        {"uses": DOWNLOAD, "with": {"pattern": "manifest-*", "path": "validation-results", "merge-multiple": True}},
        step("Verify control versus slim and every uploaded asset", "python3 .validation/verify_live_results.py --results validation-results --output validation-summary.json"),
        {"uses": UPLOAD, "if": "always()", "with": {"name": "validation-summary", "path": "validation-summary.json", "retention-days": 1}},
    ],
}
jobs["Cleanup"] = {
    "name": "Remove this run's temporary releases and tags", "runs-on": "ubuntu-latest", "if": "always()",
    "needs": all_previous + ["Verify"], "permissions": {"contents": "write"},
    "env": {"GH_TOKEN": "${{ github.token }}", "NEEDS_JSON": "${{ toJson(needs) }}"},
    "steps": [step("Clean up only known test fixtures in the fork", '''python3 - <<'PY'
import json, os, subprocess
assert os.environ['GITHUB_REPOSITORY'] == 'ofelixdev/Stockfish'
run = os.environ['GITHUB_RUN_ID']
needs = json.loads(os.environ['NEEDS_JSON'])
tags = []
for label, number in [('Latest', '1'), ('Slim', '2')]:
    tags += [f'validation-previous-{run}-{label.lower()}', f'validation-stable-{run}-{label.lower()}', f'sf_7080.{run}{number}']
    dev = needs['Dev' + label].get('outputs', {}).get('release_tag')
    if dev:
        assert dev.startswith('stockfish-dev-') and dev.endswith(f'-{run}-{label.lower()}')
        tags.append(dev)
for tag in tags:
    repo = os.environ['GITHUB_REPOSITORY']
    if subprocess.run(['gh', 'release', 'view', tag, '--repo', repo], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run(['gh', 'release', 'delete', tag, '--repo', repo, '--yes'], check=True)
    if subprocess.run(['gh', 'api', f'repos/{repo}/git/ref/tags/{tag}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run(['gh', 'api', '--method', 'DELETE', f'repos/{repo}/git/refs/tags/{tag}'], check=True)
print('Removed only the releases and tags created by this validation run')
PY
''')],
}
workflow = {
    "name": "Validate lightweight release jobs on ubuntu-slim",
    "on": {"push": {"branches": ["validate/ubuntu-slim"]}},
    "permissions": {"contents": "read", "actions": "read"},
    "concurrency": {"group": "stockfish-slim-validation", "cancel-in-progress": False},
    "jobs": jobs,
}
(TARGET / ".github/workflows/validate-slim.yml").write_text(yaml.dump(workflow, Dumper=Dumper, sort_keys=False, width=110))
print(f"Generated {len(jobs)} job definitions, including {len(matrix)} real packaging cases")
