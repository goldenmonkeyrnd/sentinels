#!/usr/bin/env python3
"""Seal exact-wheel qualification, publish immutable assets, then dispatch promotion."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
from typing import Any
import zipfile

from release_artifacts import ROOT, VERSION, release_wheel

REPOSITORY = "goldenmonkeyrnd/sentinels"
WEBSITE = "goldenmonkeyrnd/golden-monkey-rnd"
WORKFLOW = ".github/workflows/release.yml"
PYTHONS = ["3.12", "3.13", "3.14"]
JOBS = [*(f"Qualify Python {version}" for version in PYTHONS), "Qualify browser"]
TAG = f"v{VERSION}"
RECORD = "release-qualification.json"
STATE = ROOT / "dist/published-release.json"
MAX_ASSET = 25 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def command(arguments: list[str], *, data: bytes | None = None) -> bytes:
    result = subprocess.run(arguments, cwd=ROOT, input=data, capture_output=True)
    if result.returncode:
        error = result.stderr.decode(errors="replace").strip()
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(name):
                error = error.replace(os.environ[name], "[redacted]")
        raise RuntimeError(f"{arguments[0]} failed: {error[:1000]}")
    return result.stdout


def api(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    arguments = ["gh", "api", "--method", method, path]
    if payload is not None:
        arguments.extend(["--input", "-"])
    return json.loads(command(arguments, data=encoded(payload) if payload is not None else None))


def find_artifact(root: Path, name: str) -> Path:
    matches = list(root.rglob(name))
    require(len(matches) == 1, f"Expected exactly one qualified artifact: {name}")
    path = matches[0]
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()), f"Unsafe artifact path: {name}")
    return path


def source_identity() -> str:
    require(re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", VERSION) is not None, "Publish a stable package version")
    source = command(["git", "rev-parse", "HEAD"]).decode().strip()
    require(re.fullmatch(r"[a-f0-9]{40}", source) is not None, "Invalid source commit")
    require(not command(["git", "diff", "--name-only"]).strip() and not command(["git", "diff", "--cached", "--name-only"]).strip(), "Tracked release source must be clean")
    if os.environ.get("GITHUB_SHA"):
        require(os.environ["GITHUB_SHA"] == source, "Checkout does not match the workflow source SHA")
    return source


def archive_files(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(member.isfile() and not member.pax_headers and not name.is_absolute() and ".." not in name.parts and "\\" not in member.name and name.as_posix() == member.name and member.name not in files, "Browser archive contains an unsafe or duplicate entry")
            require(0 < member.size <= MAX_ASSET, f"Browser asset has an invalid size: {member.name}")
            total += member.size
            require(total <= 256 * 1024 * 1024 and len(files) < 1000, "Browser archive exceeds qualification limits")
            stream = archive.extractfile(member)
            require(stream is not None, f"Unreadable browser asset: {member.name}")
            files[member.name] = stream.read()
    return files


def prepare(artifacts: Path, output: Path, source: str) -> dict[str, dict[str, Any]]:
    """Validate supplied reports and archive bytes without calling GitHub."""
    require(artifacts.is_dir(), "Qualification artifact directory is missing")
    output.mkdir(parents=True, exist_ok=True)
    wheel = find_artifact(artifacts, f"goldenmonkey_sentinels-{VERSION}-py3-none-any.whl")
    release_wheel(wheel)
    wheel_identity = {"file": wheel.name, "sha256": sha256(wheel.read_bytes())}
    examples = {path.name: ("FAIL", 1) if path.stem.endswith("-broken") else ("PASS", 0) for path in (ROOT / "examples").glob("*.json")}
    require(len(examples) >= 6, "The guided CLI fixtures are missing")
    interpreters = []
    for version in PYTHONS:
        report = json.loads(find_artifact(artifacts, f"native-{version}.json").read_bytes())
        require(report.get("version") == 1 and report.get("wheel") == wheel_identity, f"Native {version} qualified a different wheel")
        require(re.fullmatch(re.escape(version) + r"\.\d+", report.get("python", "")) is not None, f"Native {version} has the wrong interpreter")
        require(type(report.get("tests")) is int and report["tests"] > 0 and all(type(report.get(field)) is int and report[field] == 0 for field in ("failures", "errors", "skipped")), f"Native {version} is not fully passing")
        actual = report.get("cli_examples", [])
        require(isinstance(actual, list) and len(actual) == len(examples) and len({item["bundle"] for item in actual}) == len(examples) and {item["bundle"]: (item["status"], item["exit_code"]) for item in actual} == examples, f"Native {version} did not qualify every guided CLI fixture")
        interpreters.append({key: report[key] for key in ("python", "tests", "failures", "errors", "skipped", "cli_examples")})
    require(len({item["tests"] for item in interpreters}) == 1, "Native interpreters ran different test suites")
    native = {"version": 1, "wheel": wheel_identity, "interpreters": interpreters}
    browser_path = find_artifact(artifacts, "browser-qualification.json")
    browser = json.loads(browser_path.read_bytes())
    archive = find_artifact(artifacts, f"sentinels-lab-{TAG}.tar.gz")
    files = archive_files(archive)
    require("manifest.json" in files and "assets-manifest.json" in files, "Browser archive is missing provenance")
    manifest = json.loads(files["manifest.json"])
    require(browser.get("checker_version") == VERSION and browser.get("pyodide") == "314.0.7" and isinstance(browser.get("browser"), str) and bool(browser["browser"]) and isinstance(browser.get("tests"), list) and len(browser["tests"]) >= 20, "Browser qualification is incomplete")
    require(browser.get("wheel_manifest") == manifest and manifest.get("wheel") == wheel_identity and files.get(wheel.name) == wheel.read_bytes(), "Browser reports/archive must qualify the exact native wheel")
    release = {"tag": TAG, "url": f"https://github.com/{REPOSITORY}/releases/tag/{TAG}", "source_url": f"https://github.com/{REPOSITORY}/tree/{source}", "source_archive_url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/sentinels-source-{TAG}.tar.gz"}
    require(manifest.get("version") == VERSION and manifest.get("source_commit") == source and manifest.get("source_dirty") is False and manifest.get("release") == release, "Browser provenance must match clean, exact release source")
    require(manifest.get("runtime") == "314.0.7" and manifest.get("schema_version") == json.loads((ROOT / "examples/connection-repaired.json").read_bytes())["version"], "Browser schema/runtime provenance disagrees with source")
    asset_manifest = json.loads(files["assets-manifest.json"])
    require(asset_manifest.get("version") == VERSION and set(asset_manifest.get("files", {})) == set(files) - {"assets-manifest.json"}, "Browser asset manifest has incomplete coverage")
    for name, identity in asset_manifest["files"].items():
        require(identity == {"sha256": sha256(files[name]), "bytes": len(files[name])}, f"Browser asset checksum mismatch: {name}")
    require(find_artifact(artifacts, "assets-manifest.json").read_bytes() == files["assets-manifest.json"], "Uploaded browser asset manifest differs from the archive")
    require(find_artifact(artifacts, "THIRD_PARTY_NOTICES.txt").read_bytes() == files["THIRD_PARTY_NOTICES.txt"], "Uploaded notices differ from the archive")
    corpus = json.loads(files["qualification-corpus.json"])
    require(corpus.get("wheel_sha256") == wheel_identity["sha256"] and len(corpus.get("cases", [])) >= 50 and browser.get("fixture_cases") == len(corpus["cases"]) + len(examples), "Browser qualification omitted native parity fixtures")
    for name in ("LICENSE", "NOTICE"):
        require(files.get(name) == (ROOT / name).read_bytes(), f"Browser {name} differs from release source")
    # git archive reads committed source only; deterministic gzip has no host filename/time.
    source_archive = gzip.compress(command(["git", "archive", "--format=tar", source]), mtime=0)
    prepared = {wheel.name: wheel.read_bytes(), f"goldenmonkey_sentinels-{VERSION}.tar.gz": find_artifact(artifacts, f"goldenmonkey_sentinels-{VERSION}.tar.gz").read_bytes(), archive.name: archive.read_bytes(), f"sentinels-source-{TAG}.tar.gz": source_archive, "native-qualification.json": encoded(native), "browser-qualification.json": browser_path.read_bytes(), **{name: files[name] for name in ("assets-manifest.json", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.txt")}}
    for name, content in prepared.items():
        require(bool(content), f"Empty release asset: {name}")
        (output / name).write_bytes(content)
    return {name: {"sha256": sha256(content), "bytes": len(content)} for name, content in sorted(prepared.items())}


def qualification(run_id: int, source: str, *, recorded: dict[str, Any] | None = None) -> dict[str, Any]:
    run = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
    require(run.get("id") == run_id and run.get("head_sha") == source and run.get("html_url") == run_url and run.get("path", "").split("@")[0] == WORKFLOW and run.get("repository", {}).get("full_name") == REPOSITORY and run.get("event") == "push" and run.get("head_branch") == TAG, "Qualification workflow/run/source/tag identity mismatch")
    found = []
    if recorded is not None:
        require(set(recorded) == {"run_id", "run_url", "workflow_path", "jobs"} and recorded["run_id"] == run_id and recorded["run_url"] == run_url and recorded["workflow_path"] == WORKFLOW and isinstance(recorded["jobs"], list) and len(recorded["jobs"]) == len(JOBS), "Existing qualification run identity is invalid")
        require(all(set(job) == {"id", "name", "conclusion"} and type(job["id"]) is int and job["id"] > 0 and job["name"] in JOBS and job["conclusion"] == "success" for job in recorded["jobs"]) and len({job["id"] for job in recorded["jobs"]}) == len(JOBS), "Existing qualification job identity is invalid")
        # Rerunning the original tag workflow creates new job IDs. Validate the
        # sealed attempt's explicit IDs, not whichever attempt is now latest.
        for expected in recorded["jobs"]:
            job = api(f"repos/{REPOSITORY}/actions/jobs/{expected['id']}")
            require(job.get("id") == expected["id"] and job.get("name") == expected["name"], "Existing qualification job changed identity")
            found.append(job)
    else:
        page = 1
        while True:
            batch = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}")["jobs"]
            found.extend(job for job in batch if job.get("name") in JOBS)
            if len(batch) < 100:
                break
            page += 1
            require(page <= 10, "Unexpected qualification job pagination")
    require(len(found) == len(JOBS) and {job["name"] for job in found} == set(JOBS) and len({job.get("id") for job in found}) == len(JOBS), "Required qualification jobs are missing or duplicated")
    for job in found:
        require(type(job.get("id")) is int and job["id"] > 0 and job.get("run_id") == run_id and job.get("head_sha") == source and job.get("status") == "completed" and job.get("conclusion") == "success", f"Qualification job is not complete at the exact source: {job['name']}")
    return {"run_id": run_id, "run_url": run_url, "workflow_path": WORKFLOW, "jobs": [{"id": job["id"], "name": job["name"], "conclusion": "success"} for job in sorted(found, key=lambda item: JOBS.index(item["name"]))]}


def verify_tag(source: str) -> None:
    target = api(f"repos/{REPOSITORY}/git/ref/tags/{TAG}")["object"]
    for _ in range(10):
        if target["type"] == "commit":
            require(target["sha"] == source, "Release tag points at different source")
            return
        require(target["type"] == "tag", "Release tag is not a commit or annotated tag")
        target = api(f"repos/{REPOSITORY}/git/tags/{target['sha']}")["object"]
    raise ValueError("Release tag nesting exceeds the supported limit")


def lookup_release() -> dict[str, Any] | None:
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/releases/tags/{TAG}"], cwd=ROOT, capture_output=True)
    if result.returncode:
        require(b"HTTP 404" in result.stderr, "Release lookup failed; refusing to infer absence")
        return None
    return json.loads(result.stdout)


def download_assets(release: dict[str, Any]) -> dict[str, bytes]:
    assets = release.get("assets", [])
    require(len({asset["name"] for asset in assets}) == len(assets), "Release has duplicate asset names")
    result = {}
    for asset in assets:
        require(PurePosixPath(asset["name"]).name == asset["name"] and "\\" not in asset["name"], "Release has an unsafe asset name")
        content = command(["gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset['id']}", "-H", "Accept: application/octet-stream"])
        require(len(content) == asset["size"], f"Released asset size mismatch: {asset['name']}")
        result[asset["name"]] = content
    return result


def seal(output: Path, source: str, assets: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    wheel = f"goldenmonkey_sentinels-{VERSION}-py3-none-any.whl"
    record = {"version": 1, "repository": REPOSITORY, "tag": TAG, "package_version": VERSION, "source_sha": source, "qualification": proof, "native": {"asset": "native-qualification.json", "python_versions": PYTHONS, "wheel_sha256": assets[wheel]["sha256"]}, "browser": {"asset": "browser-qualification.json", "wheel_sha256": assets[wheel]["sha256"]}, "assets": assets}
    (output / RECORD).write_bytes(encoded(record))
    return record


def checksums(output: Path, assets: dict[str, Any]) -> bytes:
    names = sorted([*assets, RECORD])
    content = "".join(f"{sha256((output / name).read_bytes())}  {name}\n" for name in names).encode()
    (output / "SHA256SUMS").write_bytes(content)
    return content


def publish(output: Path, source: str, assets: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    verify_tag(source)
    record = seal(output, source, assets, proof)
    release = lookup_release()
    if release is not None:
        require(release.get("tag_name") == TAG and release.get("prerelease") is False, "Existing release identity differs")
        existing = download_assets(release)
        require(set(existing).issubset({*assets, RECORD, "SHA256SUMS"}), "Existing release has unexpected assets")
        if RECORD in existing:
            previous = json.loads(existing[RECORD])
            expected = {key: value for key, value in record.items() if key != "qualification"}
            require({key: value for key, value in previous.items() if key != "qualification"} == expected, "Existing qualification/assets differ; immutable assets cannot be replaced")
            old_proof = previous.get("qualification", {})
            require(type(old_proof.get("run_id")) is int and old_proof == qualification(old_proof["run_id"], source, recorded=old_proof), "Existing release qualification is not verified")
            (output / RECORD).write_bytes(existing[RECORD])
        checksum_bytes = checksums(output, assets)
        for name, content in existing.items():
            require(content == (output / name).read_bytes(), f"Existing released bytes differ: {name}")
        if release.get("draft") is False:
            require(release.get("immutable") is True and set(existing) == {*assets, RECORD, "SHA256SUMS"}, "Published release must be immutable and complete")
            return release
    else:
        existing = {}
        checksum_bytes = checksums(output, assets)
        release = api(f"repos/{REPOSITORY}/releases", method="POST", payload={"tag_name": TAG, "target_commitish": source, "name": f"Sentinels {TAG}", "body": f"Qualified source-only integration checks. Native Python 3.12–3.14 and real browser Pyodide qualified the same wheel.\n\nSource: {source}\nQualification: {proof['run_url']}\n\nSee release-qualification.json and SHA256SUMS for exact evidence and asset identities. AGPL-3.0-only; corresponding source and notices are included.", "draft": True, "prerelease": False})
    require(release.get("draft") is True and type(release.get("id")) is int, "Publish only a prepared draft release")
    missing = sorted({*assets, RECORD, "SHA256SUMS"} - set(existing))
    if missing:
        command(["gh", "release", "upload", TAG, *[str(output / name) for name in missing], "--repo", REPOSITORY])
    uploaded = api(f"repos/{REPOSITORY}/releases/{release['id']}")
    expected_names = {*assets, RECORD, "SHA256SUMS"}
    downloaded = download_assets(uploaded)
    require(set(downloaded) == expected_names and all(downloaded[name] == (output / name).read_bytes() for name in expected_names), "Uploaded draft assets did not verify byte for byte")
    require(downloaded["SHA256SUMS"] == checksum_bytes, "Uploaded checksums differ")
    published = api(f"repos/{REPOSITORY}/releases/{release['id']}", method="PATCH", payload={"draft": False, "make_latest": "true"})
    require(published.get("immutable") is True and published.get("draft") is False, "Repository must seal the published release as immutable")
    return published


def write_state(output: Path, source: str, release: dict[str, Any]) -> None:
    require(output.resolve().is_relative_to(ROOT), "Published artifacts must remain within the checkout")
    state = {"version": 1, "repository": REPOSITORY, "tag": TAG, "release_id": release["id"], "source_sha": source, "immutable": True, "qualification_sha256": sha256((output / RECORD).read_bytes()), "artifacts": output.resolve().relative_to(ROOT).as_posix()}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_bytes(encoded(state))


def dispatch() -> None:
    state = json.loads(STATE.read_bytes())
    require(state.get("version") == 1 and state.get("repository") == REPOSITORY and state.get("tag") == TAG and state.get("immutable") is True and type(state.get("release_id")) is int and state["release_id"] > 0 and re.fullmatch(r"[a-f0-9]{40}", state.get("source_sha", "")) is not None, "Invalid published release state")
    path = PurePosixPath(state.get("artifacts", ""))
    require(bool(path.parts) and not path.is_absolute() and ".." not in path.parts and "\\" not in str(path), "Invalid prepared artifact directory")
    output = (ROOT / path).resolve()
    require(output.is_relative_to(ROOT), "Prepared artifacts escaped the checkout")
    content = (output / RECORD).read_bytes()
    require(sha256(content) == state.get("qualification_sha256"), "Published qualification record changed before dispatch")
    record = json.loads(content)
    names = {f"goldenmonkey_sentinels-{VERSION}-py3-none-any.whl", f"goldenmonkey_sentinels-{VERSION}.tar.gz", f"sentinels-lab-{TAG}.tar.gz", f"sentinels-source-{TAG}.tar.gz", "assets-manifest.json", "native-qualification.json", "browser-qualification.json", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.txt"}
    require(record.get("version") == 1 and record.get("repository") == REPOSITORY and record.get("tag") == TAG and record.get("package_version") == VERSION and record.get("source_sha") == state["source_sha"] and set(record.get("assets", {})) == names, "Published state/record identity differs")
    for name, identity in record["assets"].items():
        require(PurePosixPath(name).name == name and identity == {"sha256": sha256((output / name).read_bytes()), "bytes": (output / name).stat().st_size}, f"Prepared release asset changed before dispatch: {name}")
    inputs = {"operation": "promote", "sentinels_tag": TAG, "sentinels_release_id": str(state["release_id"]), "sentinels_source_sha": state["source_sha"], "sentinels_qualification_sha256": state["qualification_sha256"]}
    # The App token needs Actions write only on the website; no release/repo reads.
    command(["gh", "api", "--method", "POST", f"repos/{WEBSITE}/actions/workflows/promote-sentinels.yml/dispatches", "--input", "-"], data=encoded({"ref": "main", "inputs": inputs}))
    print(json.dumps({"dispatched": WEBSITE, "tag": TAG, "release_id": state["release_id"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / f"dist/release-assets/{TAG}")
    parser.add_argument("--validate-only", action="store_true", help="Validate and stage assets locally without GitHub API calls or publication")
    parser.add_argument("--dispatch-only", action="store_true", help="Dispatch the already published local state with an Actions-only App token")
    args = parser.parse_args()
    if args.dispatch_only:
        require(args.artifacts is None and not args.validate_only, "Dispatch mode does not accept qualification inputs")
        dispatch()
        return
    require(args.artifacts is not None, "Supply --artifacts from this workflow's downloaded qualification outputs")
    source = source_identity()
    assets = prepare(args.artifacts.resolve(), args.output.resolve(), source)
    if args.validate_only:
        print(json.dumps({"validated": True, "tag": TAG, "source_sha": source, "assets": assets}, sort_keys=True))
        return
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and re.fullmatch(r"[1-9]\d*", os.environ.get("GITHUB_RUN_ID", "")) is not None, "Publishing requires the repository's GitHub Actions run identity")
    require(args.output.resolve().is_relative_to(ROOT), "Published assets must stay within the checkout")
    proof = qualification(int(os.environ["GITHUB_RUN_ID"]), source)
    release = publish(args.output.resolve(), source, assets, proof)
    write_state(args.output.resolve(), source, release)
    print(json.dumps({"published": True, "tag": TAG, "release_id": release["id"], "source_sha": source, "qualification_sha256": sha256((args.output / RECORD).read_bytes())}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, json.JSONDecodeError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Release refused: {error}") from None
