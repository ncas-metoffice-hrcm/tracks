#!/usr/bin/env python3
"""Publish one selected directory from ncas-metoffice-hrcm/tracks to Zenodo.

The selected path can be at any depth below tropical_cyclones/ or
extratropical_cyclones/. Each distinct path gets its own Zenodo concept DOI;
subsequent publishes create new versions of that path's existing record.

This script uses Zenodo's legacy deposit API plus its current bucket upload API.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import requests

REPOSITORY_URL = "https://github.com/ncas-metoffice-hrcm/tracks"
ALLOWED_ROOTS = {"tropical_cyclones", "extratropical_cyclones"}
README_START = "<!-- ZENODO-DOI-TABLE:START -->"
README_END = "<!-- ZENODO-DOI-TABLE:END -->"
USER_AGENT = "track-data-zenodo-publisher/1.0 (https://github.com/ncas-metoffice-hrcm/tracks)"
MAX_ZENODO_FILE_BYTES = 50_000_000_000  # Zenodo's documented 50 GB per-file limit.


class PublishError(RuntimeError):
    """Expected operational error with a user-facing message."""


def run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PublishError("Git is required but was not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise PublishError(f"Git command failed: git {' '.join(args)}\n{detail}") from exc
    return result.stdout.strip()


def normalise_relative_path(raw_path: str) -> PurePosixPath:
    """Validate an input path without allowing traversal or absolute paths."""
    raw_path = raw_path.strip().replace("\\", "/")
    path = PurePosixPath(raw_path)
    if not raw_path or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PublishError("--path must be a non-empty repository-relative directory path without '.' or '..'.")
    if len(path.parts) < 2 or path.parts[0] not in ALLOWED_ROOTS:
        roots = " or ".join(sorted(ALLOWED_ROOTS))
        raise PublishError(f"--path must identify a subdirectory beneath {roots}/ (not the category root itself).")
    return path


def selected_files(repo: Path, target: Path, rel_path: PurePosixPath) -> tuple[list[Path], str]:
    """Return tracked files only, and require the selected path to be clean."""
    status = run_git(repo, "status", "--porcelain", "--", rel_path.as_posix())
    if status:
        raise PublishError(
            f"The selected directory has uncommitted changes:\n{status}\n"
            "Commit the selected directory before publishing so the Zenodo archive maps to a reproducible Git commit."
        )

    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--", rel_path.as_posix()],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    names = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    files: list[Path] = []
    for name in names:
        candidate = repo / name
        # Do not package symlinks: a symlink may point outside the selected tree.
        if candidate.is_symlink():
            print(f"WARNING: skipping symlink: {name}", file=sys.stderr)
            continue
        if candidate.is_file():
            files.append(candidate)

    if not files:
        raise PublishError(f"No tracked regular files were found under {rel_path.as_posix()}.")
    return files, run_git(repo, "rev-parse", "HEAD")


def build_archive(repo: Path, target: Path, rel_path: PurePosixPath, files: list[Path], commit: str) -> tuple[Path, str, int]:
    """Build a ZIP containing only the selected directory and provenance note."""
    temp = tempfile.NamedTemporaryFile(prefix="track-data-zenodo-", suffix=".zip", delete=False)
    archive_path = Path(temp.name)
    temp.close()

    provenance = (
        f"Repository: {REPOSITORY_URL}\n"
        f"Source directory: {rel_path.as_posix()}\n"
        f"Source commit: {commit}\n"
        f"Commit URL: {REPOSITORY_URL}/tree/{commit}/{quote(rel_path.as_posix(), safe='/')}\n"
        f"Created (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}\n"
    )

    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as zf:
            for file_path in files:
                arcname = (Path(target.name) / file_path.relative_to(target)).as_posix()
                zf.write(file_path, arcname=arcname)
            zf.writestr(f"{target.name}/ARCHIVE_INFO.txt", provenance)

        digest = hashlib.sha256()
        with archive_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        size = archive_path.stat().st_size
        if size > MAX_ZENODO_FILE_BYTES:
            raise PublishError(
                f"The ZIP is {size / 1024**3:.2f} GiB, exceeding Zenodo's documented 50 GB per-file limit."
            )
        return archive_path, digest.hexdigest(), size
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def read_metadata(path: Path, version: str, rel_path: PurePosixPath, commit: str) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PublishError(f"Metadata file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PublishError(f"Metadata file is not valid JSON: {exc}") from exc

    if not isinstance(metadata, dict):
        raise PublishError("The metadata JSON must contain one JSON object.")

    for required in ("title", "description", "creators"):
        if not metadata.get(required):
            raise PublishError(f"Metadata is missing required field '{required}'.")
    if not isinstance(metadata["creators"], list) or not metadata["creators"]:
        raise PublishError("Metadata 'creators' must be a non-empty list of Zenodo creator objects.")
    if any(not isinstance(c, dict) or not c.get("name") for c in metadata["creators"]):
        raise PublishError("Every creator must be an object with a non-empty 'name'.")

    metadata["upload_type"] = "dataset"
    metadata["version"] = version
    metadata.setdefault("publication_date", dt.date.today().isoformat())
    metadata.setdefault("access_right", "open")
    if metadata["access_right"] in {"open", "embargoed"} and not metadata.get("license"):
        raise PublishError("Metadata must specify a Zenodo license identifier when access_right is open or embargoed.")
    if metadata["access_right"] == "embargoed" and not metadata.get("embargo_date"):
        raise PublishError("Metadata must specify embargo_date when access_right is embargoed.")
    if metadata["access_right"] == "restricted" and not metadata.get("access_conditions"):
        raise PublishError("Metadata must specify access_conditions when access_right is restricted.")

    # Make the source tree and exact commit discoverable from the Zenodo record.
    source_url = f"{REPOSITORY_URL}/tree/{commit}/{quote(rel_path.as_posix(), safe='/')}"
    related = list(metadata.get("related_identifiers", []))
    if not any(item.get("identifier") == source_url for item in related if isinstance(item, dict)):
        related.append({"identifier": source_url, "relation": "isDerivedFrom"})
    metadata["related_identifiers"] = related
    return metadata


def api_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        response = session.request(method, url, json=json_body, data=data, headers=headers, timeout=(30, 600))
    except requests.RequestException as exc:
        raise PublishError(f"Zenodo API request failed: {exc}") from exc
    if not response.ok:
        body = response.text[:4000]
        raise PublishError(f"Zenodo API returned HTTP {response.status_code} for {method} {url}:\n{body}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    })
    return session


def clear_draft_files(session: requests.Session, api_base: str, draft: dict[str, Any]) -> None:
    """Remove inherited files from a new-version draft before uploading the new snapshot."""
    draft_id = draft["id"]
    for file_info in draft.get("files", []):
        file_id = file_info.get("id")
        if not file_id:
            raise PublishError(
                "Zenodo returned an inherited file without a deposition file id; refusing to risk publishing a mixed archive."
            )
        api_request(
            session,
            "DELETE",
            f"{api_base}/deposit/depositions/{draft_id}/files/{quote(str(file_id), safe='')}",
        )


def upload_archive(session: requests.Session, draft: dict[str, Any], archive_path: Path, filename: str) -> None:
    bucket = draft.get("links", {}).get("bucket")
    if not bucket:
        raise PublishError("Zenodo draft response did not contain a bucket upload URL.")
    with archive_path.open("rb") as stream:
        api_request(
            session,
            "PUT",
            f"{bucket.rstrip('/')}/{quote(filename, safe='')}",
            data=stream,
            headers={"Content-Type": "application/zip"},
        )


def publish_to_zenodo(
    metadata: dict[str, Any], archive_path: Path, filename: str,
    existing_entry: dict[str, Any] | None, sandbox: bool,
) -> dict[str, Any]:
    token = os.environ.get("ZENODO_ACCESS_TOKEN", "").strip()
    if not token:
        raise PublishError("Set ZENODO_ACCESS_TOKEN in the environment; do not put the token in the repository.")
    host = "https://sandbox.zenodo.org" if sandbox else "https://zenodo.org"
    api_base = f"{host}/api"
    session = make_session(token)

    if existing_entry:
        latest_id = existing_entry.get("latest_deposition_id")
        if not latest_id:
            raise PublishError("Registry entry is missing latest_deposition_id; cannot safely create a new version.")
        old = api_request(
            session,
            "POST",
            f"{api_base}/deposit/depositions/{latest_id}/actions/newversion",
        )
        draft_url = old.get("links", {}).get("latest_draft")
        if not draft_url:
            raise PublishError("Zenodo did not return links.latest_draft after requesting a new version.")
        draft = api_request(session, "GET", draft_url)
        clear_draft_files(session, api_base, draft)
        action = "new-version draft"
    else:
        draft = api_request(session, "POST", f"{api_base}/deposit/depositions", json_body={})
        action = "new record draft"

    draft_id = draft["id"]
    api_request(
        session,
        "PUT",
        f"{api_base}/deposit/depositions/{draft_id}",
        json_body={"metadata": metadata},
    )
    upload_archive(session, draft, archive_path, filename)

    print(f"Created {action} {draft_id}; publishing to Zenodo...")
    published = api_request(
        session,
        "POST",
        f"{api_base}/deposit/depositions/{draft_id}/actions/publish",
    )
    # The publish response may omit identifiers documented on the draft. Keep
    # the values returned when the draft was created as safe fallbacks.
    if isinstance(published, dict):
        published.setdefault("conceptrecid", draft.get("conceptrecid"))
        published.setdefault("conceptdoi", draft.get("conceptdoi"))
        reserved = draft.get("metadata", {}).get("prereserve_doi", {}).get("doi")
        if reserved:
            published.setdefault("doi", reserved)
    return published


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"Could not read registry {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("records", {}), dict):
        raise PublishError(f"Registry has an unexpected structure: {path}")
    value.setdefault("schema_version", 1)
    value.setdefault("records", {})
    return value


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def make_registry_entry(
    published: dict[str, Any], metadata: dict[str, Any], rel_path: PurePosixPath,
    commit: str, archive_sha256: str, archive_size: int, sandbox: bool,
) -> dict[str, Any]:
    host = "https://sandbox.zenodo.org" if sandbox else "https://zenodo.org"
    doi = published.get("doi")
    concept_doi = published.get("conceptdoi")
    if not concept_doi and published.get("conceptrecid"):
        prefix = "10.5072" if sandbox else "10.5281"
        concept_doi = f"{prefix}/zenodo.{published['conceptrecid']}"
    record_id = published.get("record_id") or published.get("id")
    if not doi:
        raise PublishError("Zenodo reported publish success but returned no version DOI; check your Zenodo uploads before retrying.")
    if not concept_doi:
        raise PublishError("Zenodo published the record but returned no concept DOI; check the record before retrying.")
    return {
        "title": metadata["title"],
        "latest_deposition_id": published.get("id") or record_id,
        "record_id": record_id,
        "concept_doi": concept_doi,
        "latest_doi": doi,
        "record_url": published.get("record_url") or f"{host}/records/{record_id}",
        "latest_version": metadata["version"],
        "source_commit": commit,
        "archive_sha256": archive_sha256,
        "archive_size_bytes": archive_size,
        "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def update_readme(readme_path: Path, records: dict[str, Any]) -> None:
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    current = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    rows = [
        "| Dataset | Source directory | Latest version | Concept DOI (all versions) | Version DOI |",
        "|---|---|---:|---|---|",
    ]
    for directory, entry in sorted(records.items()):
        title = str(entry.get("title", directory)).replace("|", "\\|").replace("\n", " ")
        safe_path = directory.replace("|", "\\|")
        source_url = f"{REPOSITORY_URL}/tree/main/{quote(directory, safe='/')}"
        concept = str(entry["concept_doi"])
        latest = str(entry["latest_doi"])
        version = str(entry.get("latest_version", ""))
        rows.append(
            f"| {title} | [`{safe_path}`]({source_url}) | {version} "
            f"| [<code>{concept}</code>](https://doi.org/{concept}) "
            f"| [<code>{latest}</code>](https://doi.org/{latest}) |"
        )
    block = "\n".join([README_START, "## Zenodo DOIs", "", *rows, README_END])

    if README_START in current and README_END in current:
        before = current.split(README_START, 1)[0].rstrip()
        after = current.split(README_END, 1)[1].lstrip()
        updated = f"{before}\n\n{block}\n"
        if after:
            updated += f"\n{after}"
    else:
        updated = current.rstrip() + ("\n\n" if current.strip() else "") + block + "\n"
    readme_path.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish one directory of tracks to its own versioned Zenodo dataset record."
    )
    parser.add_argument("--path", required=True,
                        help="Directory relative to repository root, at any depth under tropical_cyclones/ or extratropical_cyclones/.")
    parser.add_argument("--metadata-file", required=True,
                        help="JSON file containing the Zenodo metadata for this directory.")
    parser.add_argument("--version", required=True, help="Version label, e.g. 1.0.0.")
    parser.add_argument("--repo-root", default=".", help="Path to the checked-out tracks repository (default: current directory).")
    parser.add_argument("--registry", default="zenodo/records.json", help="Path to the path-to-Zenodo-record registry.")
    parser.add_argument("--readme", default="README.md", help="README to update with the DOI table. Use --no-readme to skip.")
    parser.add_argument("--no-readme", action="store_true", help="Do not update README.md.")
    parser.add_argument("--sandbox", action="store_true", help="Publish to Zenodo Sandbox instead of production Zenodo.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and build the ZIP without calling Zenodo or updating the registry.")
    args = parser.parse_args()

    repo = Path(args.repo_root).resolve()
    rel_path = normalise_relative_path(args.path)
    target = (repo / Path(*rel_path.parts)).resolve()
    try:
        target.relative_to(repo)
    except ValueError as exc:
        raise PublishError("Selected directory resolves outside the repository.") from exc
    if not target.is_dir():
        raise PublishError(f"Selected directory does not exist: {rel_path.as_posix()}")

    files, commit = selected_files(repo, target, rel_path)
    metadata_path = Path(args.metadata_file)
    if not metadata_path.is_absolute():
        metadata_path = repo / metadata_path
    metadata = read_metadata(metadata_path, args.version, rel_path, commit)

    archive_path, archive_sha256, archive_size = build_archive(repo, target, rel_path, files, commit)
    try:
        print(f"Selected directory : {rel_path.as_posix()}")
        print(f"Tracked files      : {len(files)}")
        print(f"Git commit         : {commit}")
        print(f"Archive size       : {archive_size / 1024**2:.1f} MiB")
        print(f"Archive SHA-256    : {archive_sha256}")
        print(f"Zenodo title       : {metadata['title']}")
        print(f"Version            : {metadata['version']}")

        if args.dry_run:
            print("DRY RUN: no upload, DOI, registry or README changes were made.")
            return 0

        registry_path = Path(args.registry)
        if not registry_path.is_absolute():
            registry_path = repo / registry_path
        registry = load_registry(registry_path)
        registry_key = rel_path.as_posix()
        existing = registry["records"].get(registry_key)
        published = publish_to_zenodo(metadata, archive_path, f"{target.name}.zip", existing, args.sandbox)
        entry = make_registry_entry(published, metadata, rel_path, commit, archive_sha256, archive_size, args.sandbox)
        registry["records"][registry_key] = entry
        save_json_atomic(registry_path, registry)

        if not args.no_readme and not args.sandbox:
            readme_path = Path(args.readme)
            if not readme_path.is_absolute():
                readme_path = repo / readme_path
            update_readme(readme_path, registry["records"])

        print("\nPublished successfully:")
        print(f"  Concept DOI: https://doi.org/{entry['concept_doi']}")
        print(f"  Version DOI: https://doi.org/{entry['latest_doi']}")
        print(f"  Record URL : {entry['record_url']}")
        print(f"  Registry   : {registry_path}")
        if not args.no_readme and not args.sandbox:
            print(f"  README     : {readme_path}")
        return 0
    finally:
        archive_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
