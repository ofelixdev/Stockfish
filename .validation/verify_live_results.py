#!/usr/bin/env python3
"""Compare both runners and verify the bytes downloaded from the fork's releases."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def fetch_release(repo, tag):
    # gh resolves draft tags through GraphQL; the REST tag endpoint only serves
    # published releases. Fetch the full REST payload by the resolved numeric ID.
    identity = gh_json("release", "view", tag, "--repo", repo, "--json", "databaseId")
    release = gh_json("api", f"repos/{repo}/releases/{identity['databaseId']}")
    assert release["tag_name"] == tag, (tag, release["tag_name"])
    return release


def verify(results, summary):
    repo = os.environ["GITHUB_REPOSITORY"]
    assert repo == "ofelixdev/Stockfish", repo
    needs = json.loads(os.environ["NEEDS_JSON"])
    unsuccessful = {name: value["result"] for name, value in needs.items() if value["result"] != "success"}
    assert not unsuccessful, unsuccessful
    summary["jobs"] = {name: value["result"] for name, value in needs.items()}
    reference = needs["MatrixLatest"]["outputs"]
    for name in ("MatrixLatest", "MatrixSlim", "OfficialMatrixLatest", "OfficialMatrixSlim"):
        for kind in ("arm_matrix", "universal_matrix"):
            assert json.loads(needs[name]["outputs"][kind]) == json.loads(reference[kind]), (name, kind)
    summary["matrix_comparisons"] = 8
    meta_paths = list(results.glob("*.meta.json"))
    assert len(meta_paths) == 16, f"Expected 16 metadata files, found {len(meta_paths)}"
    metadata = [json.loads(path.read_text()) for path in meta_paths]
    cases = sorted({item["case_id"] for item in metadata})
    assert len(cases) == 8, cases
    summary["payloads"] = []
    for case in cases:
        latest = json.loads((results / f"latest-{case}.json").read_text())
        slim = json.loads((results / f"slim-{case}.json").read_text())
        assert latest == slim, f"Payload differs across runner types: {case}"
        summary["payloads"].append({"case": case, "files_verified": len(latest["files"]), "same_bytes_and_modes": True})
        print(f"Identical content and permissions: {case} ({len(latest['files'])} files)", flush=True)
    summary["release_assets"] = []
    for label in ("Latest", "Slim"):
        expected = {item["archive"]: item for item in metadata if item["label"] == label.lower()}
        assert len(expected) == 8
        for kind, draft, prerelease in (("Dev", False, True), ("Official", True, False)):
            tag = needs[kind + label]["outputs"]["release_tag"]
            release = fetch_release(repo, tag)
            assert release["draft"] is draft and release["prerelease"] is prerelease, (tag, release["draft"], release["prerelease"])
            assert {asset["name"] for asset in release["assets"]} == set(expected), tag
            assert len(release["assets"]) == 8
            for asset in release["assets"]:
                wanted = expected[asset["name"]]
                assert asset["size"] == wanted["size"], asset["name"]
                with tempfile.TemporaryDirectory() as directory:
                    subprocess.run(["gh", "release", "download", tag, "--repo", repo,
                                    "--pattern", asset["name"], "--dir", directory], check=True)
                    with (Path(directory) / asset["name"]).open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                assert digest == wanted["sha256"], (tag, asset["name"])
                summary["release_assets"].append({"tag": tag, "name": asset["name"], "size": asset["size"], "sha256": digest})
                print(f"Downloaded and verified: {tag}/{asset['name']}", flush=True)
    summary["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = {"success": False, "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        verify(args.results, summary)
    except Exception as error:
        summary["error"] = repr(error)
        raise
    finally:
        args.output.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
