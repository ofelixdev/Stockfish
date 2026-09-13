"""Exercise real archive contents and failure cases without external packages."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import warnings
import zipfile

import verify_package as verifier


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = self.base / "fixture"
        self.wiki = self.base / "wiki"
        self.fixture.mkdir()
        self.wiki.mkdir()
        self.binary = "stockfish-linux-x86-64-universal"
        inputs = {
            "src/main.cpp": b"int main() {}\n",
            "src/nested/net.nnue": bytes(range(256)),
            "scripts/tool.sh": b"#!/bin/sh\necho check\n",
            **{name: f"Contents of {name}\n".encode() for name in verifier.COPY_FILES},
            self.binary: b"ELF-example-binary\x00\xff",
        }
        for name, data in inputs.items():
            path = self.fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        (self.wiki / "Home.md").write_text("Wiki snapshot\n")
        (self.wiki / "Home.md").chmod(0o644)
        (self.wiki / "empty").mkdir()

    def build(self, kind, mutate=None):
        stage = self.base / f"stage-{kind}"
        root = stage / "stockfish"
        shutil.copytree(self.fixture, root)
        shutil.copytree(self.wiki, root / "wiki")
        if kind == "tar":
            (root / self.binary).chmod(0o755)
        if mutate:
            mutate(root)
        archive = self.base / ("release.tar.gz" if kind == "tar" else "release.zip")
        if kind == "tar":
            with tarfile.open(archive, "w:gz") as package:
                package.add(root, arcname="stockfish")
        else:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                package.write(root, "stockfish")
                for path in sorted(root.rglob("*")):
                    package.write(path, path.relative_to(stage).as_posix())
        return archive

    def run_verify(self, archive, success=True):
        manifest = self.base / f"{archive.name}.json"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = verifier.main([
                "verify", "--fixture", str(self.fixture), "--wiki", str(self.wiki),
                "--archive", str(archive), "--binary-name", self.binary,
                "--manifest", str(manifest),
            ])
        self.assertEqual(result, 0 if success else 1, output.getvalue())
        if success:
            self.assertTrue(manifest.is_file())
            return json.loads(manifest.read_text())
        self.assertFalse(manifest.exists(), "Invalid archive must not emit a success manifest")
        return output.getvalue()

    def test_valid_tar_and_zip(self):
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                manifest = self.run_verify(self.build(kind))
                self.assertEqual(len(manifest["files"]), 11)
                binary = next(x for x in manifest["files"] if x["path"].endswith(self.binary))
                self.assertEqual(binary["mode"], "0755" if kind == "tar" else "0644")
                self.assertIn("stockfish/wiki/empty", manifest["directories"])

    def test_missing_file(self):
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                archive = self.build(kind, lambda root: (root / "src/main.cpp").unlink())
                self.assertIn("missing=", self.run_verify(archive, success=False))

    def test_same_size_corruption(self):
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                archive = self.build(kind, lambda root: (root / "src/nested/net.nnue").write_bytes(
                    bytes(reversed(range(256)))
                ))
                self.assertIn("sha256 mismatch", self.run_verify(archive, success=False))

    def test_tar_lost_executable_permission(self):
        archive = self.build("tar", lambda root: (root / self.binary).chmod(0o644))
        self.assertIn("mode mismatch", self.run_verify(archive, success=False))

    def test_other_file_mode_change(self):
        archive = self.build("zip", lambda root: (root / "README.md").chmod(0o600))
        self.assertIn("mode mismatch", self.run_verify(archive, success=False))

    def test_extra_file(self):
        archive = self.build("tar", lambda root: (root / "unexpected.txt").write_text("extra"))
        self.assertIn("extra=", self.run_verify(archive, success=False))

    def test_git_metadata_rejected(self):
        archive = self.build("tar", lambda root: (root / ".git").mkdir())
        self.assertIn("Unsafe", self.run_verify(archive, success=False))

    def test_tar_symlink_rejected(self):
        archive = self.build("tar", lambda root: (root / "leak").symlink_to("../fixture"))
        self.assertIn("Non-regular TAR", self.run_verify(archive, success=False))

    def test_zip_traversal_rejected(self):
        archive = self.build("zip")
        with zipfile.ZipFile(archive, "a") as package:
            entry = zipfile.ZipInfo("stockfish/../escape")
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            package.writestr(entry, "outside")
        self.assertIn("Unsafe", self.run_verify(archive, success=False))

    def test_zip_symlink_rejected(self):
        archive = self.build("zip")
        with zipfile.ZipFile(archive, "a") as package:
            entry = zipfile.ZipInfo("stockfish/link")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            package.writestr(entry, "../outside")
        self.assertIn("non-regular ZIP", self.run_verify(archive, success=False))

    def test_duplicate_entries_rejected(self):
        archive = self.build("zip")
        with zipfile.ZipFile(archive, "a") as package, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            entry = zipfile.ZipInfo("stockfish/README.md")
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            package.writestr(entry, (self.fixture / "README.md").read_bytes())
        self.assertIn("Duplicate archive entry", self.run_verify(archive, success=False))

    def test_tar_and_zip_command_line_archives(self):
        if not shutil.which("tar") or not shutil.which("zip"):
            self.skipTest("tar and zip executables are needed for workflow-style packaging")
        for kind in ("tar", "zip"):
            with self.subTest(kind=kind):
                archive = self.build(kind)
                archive.unlink()
                command = (["tar", "-czf", str(archive), "stockfish"] if kind == "tar"
                           else ["zip", "-rq", str(archive), "stockfish"])
                # macOS tar otherwise adds AppleDouble files absent on Linux runners.
                environment = dict(os.environ, COPYFILE_DISABLE="1")
                subprocess.run(command, cwd=self.base / f"stage-{kind}", check=True,
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.run_verify(archive)

    def test_manifests_ignore_archive_timestamp_but_detect_byte_changes(self):
        archive = self.build("tar")
        self.run_verify(archive)
        first = self.base / "first.json"
        shutil.copyfile(self.base / f"{archive.name}.json", first)
        # An archive timestamp is not part of the normalized payload manifest.
        root = self.base / "stage-tar/stockfish"
        with tarfile.open(archive, "w:gz") as package:
            def shifted_timestamp(member):
                member.mtime += 100
                return member
            package.add(root, arcname="stockfish", filter=shifted_timestamp)
        self.run_verify(archive)
        second = self.base / f"{archive.name}.json"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(["compare", str(first), str(second)]), 0)
            changed = json.loads(second.read_text())
            changed["files"][0]["sha256"] = "0" * 64
            second.write_text(json.dumps(changed))
            self.assertEqual(verifier.main(["compare", str(first), str(second)]), 1)


if __name__ == "__main__":
    unittest.main()
