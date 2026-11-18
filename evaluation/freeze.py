"""Record and verify SHA-256 digests for inputs, labels, settings, and checkpoint source."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
DATA_MANIFEST = Path("evaluation/frozen_dataset.json")
IMPLEMENTATION_MANIFEST = Path("evaluation/frozen_implementations.json")
DATA_FILES = (
    Path("evaluation/labels.json"),
    Path("evaluation/settings.json"),
    Path("evaluation/generate.py"),
    Path("evaluation/oracle.py"),
    Path("evaluation/fake_transcripts.py"),
)


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_digests(root, paths):
    return {str(path): sha256_file(root / path) for path in sorted(set(paths))}


def load_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def freeze_dataset(root):
    paths = [*DATA_FILES, *(item.relative_to(root) for item in (root / "fixtures/inputs").glob("*.json"))]
    if len(paths) <= len(DATA_FILES):
        raise ValueError("No generated case inputs found")
    write_json(root / DATA_MANIFEST, {"algorithm": "sha256", "files": file_digests(root, paths)})


def verify_files(root, expected):
    problems = []
    for path, digest in expected.items():
        full_path = root / path
        if not full_path.is_file():
            problems.append(f"missing {path}")
        elif sha256_file(full_path) != digest:
            problems.append(f"modified {path}")
    if problems:
        raise ValueError("Freeze check failed: " + ", ".join(problems))


def verify_dataset(root):
    manifest = load_json(root / DATA_MANIFEST)
    if manifest["algorithm"] != "sha256":
        raise ValueError("Unrecognized dataset hash algorithm")
    actual_inputs = {item.relative_to(root).as_posix() for item in (root / "fixtures/inputs").glob("*.json")}
    expected_paths = {path.as_posix() for path in DATA_FILES} | actual_inputs
    if set(manifest["files"]) != expected_paths:
        raise ValueError("Dataset manifest membership differs from local files")
    verify_files(root, manifest["files"])


def verify_checkpoints(root):
    manifest = load_json(root / IMPLEMENTATION_MANIFEST)
    if manifest["algorithm"] != "sha256":
        raise ValueError("Unrecognized checkpoint hash algorithm")
    final_paths = {
        path.relative_to(root).as_posix() for path in (root / "reviewer").rglob("*.py")
    }
    if set(manifest["baseline"]["files"]) != {"baseline.py"} or set(manifest["final"]["files"]) != final_paths:
        raise ValueError("Implementation manifest membership differs from local source files")
    for checkpoint in ("baseline", "final"):
        checkpoint_data = manifest[checkpoint]
        if not checkpoint_data["files"]:
            raise ValueError(f"No source hashes are available for {checkpoint}")
        verify_files(root, checkpoint_data["files"])
    return manifest


def _git(root, *arguments):
    return subprocess.run(["git", *arguments], cwd=root, check=True, stdout=subprocess.PIPE).stdout


def record_checkpoints(root, baseline, final):
    """Hash committed bytes and verify local reviewers match before writing a manifest."""
    baseline_commit = _git(root, "rev-parse", "--verify", f"{baseline}^{{commit}}").decode().strip()
    final_commit = _git(root, "rev-parse", "--verify", f"{final}^{{commit}}").decode().strip()
    final_paths = [
        path for path in _git(root, "ls-tree", "-r", "-z", "--name-only", final_commit, "--", "reviewer")
        .decode().split("\0") if path.endswith(".py")
    ]
    if not final_paths:
        raise ValueError("No reviewer Python files found in the selected final checkpoint")
    manifest = {"algorithm": "sha256"}
    for name, commit, paths in [("baseline", baseline_commit, ["baseline.py"]),
                                ("final", final_commit, final_paths)]:
        files = {path: hashlib.sha256(_git(root, "show", f"{commit}:{path}")).hexdigest()
                 for path in paths}
        verify_files(root, files)
        manifest[name] = {"commit": commit, "files": files}
    write_json(root / IMPLEMENTATION_MANIFEST, manifest)
    verify_checkpoints(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record-dataset", "record-implementations",
                                           "verify-dataset", "verify-all"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--baseline")
    parser.add_argument("--final")
    args = parser.parse_args()
    if args.action == "record-dataset":
        freeze_dataset(args.root)
    elif args.action == "record-implementations":
        if not args.baseline or not args.final:
            parser.error("record-implementations requires --baseline and --final")
        record_checkpoints(args.root, args.baseline, args.final)
    else:
        verify_dataset(args.root)
        if args.action == "verify-all":
            verify_checkpoints(args.root)
    print(f"{args.action} OK")


if __name__ == "__main__":
    main()
