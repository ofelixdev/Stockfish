#!/usr/bin/env python3
"""Verify Stockfish release payloads and compare normalized verification manifests."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
import zipfile


ROOT = "stockfish"
COPY_FILES = (
    "Top CPU Contributors.txt", "Copying.txt", "AUTHORS", "CITATION.cff",
    "README.md", "CONTRIBUTING.md",
)


class ValidationError(Exception):
    """An archive does not faithfully represent the supplied inputs."""


def fingerprint(stream):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def safe_name(name):
    """Validate without normalizing away potentially dangerous components."""
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed or "\\" in name or "\x00" in name
        or any(part in ("", ".", "..", ".git") for part in parts)
        or parts[0] != ROOT
    ):
        raise ValidationError(f"Unsafe or unexpected archive path: {name!r}")
    return trimmed


def expected_payload(fixture, wiki, binary_name, archive_kind):
    if PurePosixPath(binary_name).name != binary_name or "\\" in binary_name:
        raise ValidationError("--binary-name must be one filename, without directories")
    if binary_name in ("", ".", "..", ".git"):
        raise ValidationError("Invalid --binary-name")
    expected = {}
    directories = {ROOT}

    def add(source, destination):
        safe_name(destination)
        if source.is_symlink():
            raise ValidationError(f"Symlink in expected payload: {source}")
        if source.is_dir():
            directories.add(destination)
            for child in sorted(source.iterdir()):
                add(child, f"{destination}/{child.name}")
        elif source.is_file():
            with source.open("rb") as stream:
                entry = fingerprint(stream)
            mode = stat.S_IMODE(source.stat().st_mode)
            if destination == f"{ROOT}/{binary_name}" and archive_kind == "tar":
                mode |= 0o111  # upload_binaries.yml runs chmod +x before tar.
            entry["mode"] = f"{mode:04o}"
            if destination in expected:
                raise ValidationError(f"Duplicate expected file: {destination}")
            expected[destination] = entry
        else:
            raise ValidationError(f"Missing or non-regular input: {source}")

    for name in ("src", "scripts"):
        source = fixture / name
        if not source.is_dir():
            raise ValidationError(f"Required input directory missing: {source}")
        add(source, f"{ROOT}/{name}")
    for name in (*COPY_FILES, binary_name):
        source = fixture / name
        if not source.is_file():
            raise ValidationError(f"Required input file missing: {source}")
        add(source, f"{ROOT}/{name}")
    if not wiki.is_dir():
        raise ValidationError(f"Required wiki directory missing: {wiki}")
    add(wiki, f"{ROOT}/wiki")
    return expected, directories


def read_archive(archive, archive_kind):
    files, directories, seen = {}, set(), set()

    def record(name, is_directory, mode, stream=None):
        name = safe_name(name)
        if name in seen:
            raise ValidationError(f"Duplicate archive entry: {name}")
        seen.add(name)
        if is_directory:
            directories.add(name)
            return
        if stream is None:
            raise ValidationError(f"Cannot read archive file: {name}")
        with stream:
            files[name] = fingerprint(stream)
        files[name]["mode"] = f"{stat.S_IMODE(mode):04o}"

    if archive_kind == "zip":
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if member.create_system != 3 or kind not in (stat.S_IFDIR, stat.S_IFREG):
                    raise ValidationError(f"Non-Unix or non-regular ZIP entry: {member.filename}")
                if member.is_dir() != (kind == stat.S_IFDIR):
                    raise ValidationError(f"Inconsistent ZIP entry type: {member.filename}")
                record(member.filename, member.is_dir(), mode,
                       None if member.is_dir() else package.open(member))
    else:
        with tarfile.open(archive, "r:*") as package:
            for member in package:
                if not (member.isdir() or member.isfile()) or member.issparse():
                    raise ValidationError(f"Non-regular TAR entry: {member.name}")
                record(member.name, member.isdir(), member.mode,
                       None if member.isdir() else package.extractfile(member))
    return files, directories


def same_sets(expected, actual, description):
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        raise ValidationError(f"{description}: missing={missing}; extra={extra}")


def verify(args):
    kind = "zip" if args.archive.suffix.lower() == ".zip" else "tar"
    expected, expected_dirs = expected_payload(args.fixture, args.wiki, args.binary_name, kind)
    actual, actual_dirs = read_archive(args.archive, kind)
    same_sets(expected, actual, "Payload files differ")
    same_sets(expected_dirs, actual_dirs, "Payload directories differ")
    for name in sorted(expected):
        for field in ("size", "sha256", "mode"):
            if expected[name][field] != actual[name][field]:
                raise ValidationError(
                    f"{name}: {field} mismatch "
                    f"(expected {expected[name][field]}, got {actual[name][field]})"
                )
    manifest = {
        "schema": 1,
        "archive_kind": kind,
        "binary_name": args.binary_name,
        "directories": sorted(actual_dirs),
        "files": [{"path": name, **actual[name]} for name in sorted(actual)],
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Verified {len(actual)} files in {args.archive}; manifest: {args.manifest}")


def compare(args):
    first = json.loads(args.first.read_text())
    second = json.loads(args.second.read_text())
    if first != second:
        raise ValidationError("Normalized manifests differ (paths, bytes, modes, or format)")
    print(f"Identical verified payloads: {args.first} and {args.second}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser(
        "verify", help="Compare an actual release archive against preserved compilation inputs and wiki"
    )
    check.add_argument("--fixture", required=True, type=Path,
                       help="Preserved compilation artifact after actions/download-artifact")
    check.add_argument("--wiki", required=True, type=Path,
                       help="The exact wiki tree packaged by the workflow, with .git removed")
    check.add_argument("--archive", required=True, type=Path, help="Actual .tar.gz or Unix ZIP package")
    check.add_argument("--binary-name", required=True, help="Exact executable basename in fixture")
    check.add_argument("--manifest", required=True, type=Path, help="Verified normalized JSON output")
    check.set_defaults(run=verify)
    comparison = subcommands.add_parser(
        "compare", help="Compare verified manifests, for example ubuntu-latest versus ubuntu-slim"
    )
    comparison.add_argument("first", type=Path)
    comparison.add_argument("second", type=Path)
    comparison.set_defaults(run=compare)
    args = parser.parse_args(argv)
    try:
        args.run(args)
    except (ValidationError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
