# Publishing individual `tracks` directories to Zenodo

This toolkit is intended to be copied into `ncas-metoffice-hrcm/tracks`.
It publishes one selected directory at any depth below `tropical_cyclones/`
or `extratropical_cyclones/` as a separate Zenodo **dataset** record. Each
selected path gets its own Concept DOI and its own version-specific DOIs.

## What gets published

The publisher takes an exact repository-relative directory path, for example:

```text
tropical_cyclones/model_simulations/HadGEM3/TRACK/u-ch330/NH
extratropical_cyclones/reanalysis/ERA5/TRACK/NH
```

These are examples only; use a directory that exists in the checked-out repo.
The script supports any depth below either allowed root. It archives tracked,
regular files in that directory, preserving the directory's own name and its
internal structure in a ZIP. It skips symlinks, excludes untracked files, and
requires the selected directory to be clean in Git. Git LFS content is fetched
by the supplied Actions workflow.

The archive contains an `ARCHIVE_INFO.txt` file recording the source directory,
repository commit, and immutable GitHub link to that exact commit and directory.

## Install in the repository

Copy these files into the root of `ncas-metoffice-hrcm/tracks`:

```text
scripts/publish_zenodo_directory.py
.github/workflows/publish-zenodo-directory.yml
zenodo/metadata/example-dataset.json
```

Commit them to the default branch. You can rename/copy `example-dataset.json`
for each dataset you plan to publish. Metadata files should be committed to the
repository before launching the workflow.

## Metadata per published directory

Create one JSON file for each directory, for example:

```text
zenodo/metadata/tropical-cyclones-hadgem3-track-u-ch330-nh.json
```

Start from `zenodo/metadata/example-dataset.json` and replace the placeholders.
At minimum, provide:

- `title`
- `description`
- `creators` (a non-empty list; creator names are required)
- `license` (a Zenodo license identifier if the record is open or embargoed)

The example uses `mit` because the repository currently advertises an MIT
licence, but verify that this licence is legally appropriate for the selected
data before publishing. Do not assume the repository licence necessarily
covers every third-party dataset or contribution.

Other Zenodo metadata fields such as `keywords`, `related_identifiers`,
`dates`, `locations`, `contributors`, `grants`, and `references` can be added
using the Zenodo Deposit API field names.

## Configure secrets

Create two Zenodo access tokens and add them as GitHub Actions repository
secrets:

- `ZENODO_SANDBOX_ACCESS_TOKEN`: token created at https://sandbox.zenodo.org
- `ZENODO_ACCESS_TOKEN`: production token created at https://zenodo.org

The tokens need the `deposit:write` and `deposit:actions` scopes. Do not commit
tokens to the repository. Sandbox and production have separate accounts/tokens
and separate DOI namespaces.

## Publish from GitHub Actions

1. Open the repository's **Actions** tab.
2. Select **Publish selected directory to Zenodo**.
3. Choose **Run workflow**.
4. Enter the full path relative to the repository root, e.g.
   `tropical_cyclones/model_simulations/HadGEM3/TRACK/u-ch330/NH`.
5. Enter that directory's metadata JSON path.
6. Enter a version, e.g. `1.0.0`.
7. Choose `sandbox` first. Once tested, choose `production` to mint a public DOI.

For production, the workflow updates `zenodo/records.json` and the generated
DOI table in the root `README.md`, then commits those changes. Sandbox runs
update the separate `zenodo/records-sandbox.json` registry but do not update
the README; this lets repeated Sandbox tests create new versions of the same
test record without mixing test DOIs with production DOIs.

## Versioning model

- First publication of a path creates its Zenodo record and Concept DOI.
- Publishing the same path again creates a new version of that record.
- Publishing a different path creates a different Zenodo record and Concept DOI.
- The README table links both the Concept DOI (latest version) and the
  version-specific DOI (fixed snapshot).
- The registry maps the exact path string to the latest Zenodo deposition ID;
  keep `zenodo/records.json` under version control.

Use a different metadata file for each path. The workflow deliberately asks for
the version on each run so unrelated directories can be released independently.

## Test locally before production

Install the one Python dependency:

```bash
python -m pip install requests
```

From the repository root, test the ZIP and metadata without contacting Zenodo:

```bash
python scripts/publish_zenodo_directory.py \
  --path tropical_cyclones/model_simulations/HadGEM3/TRACK/u-ch330/NH \
  --metadata-file zenodo/metadata/tropical-cyclones-hadgem3-track-u-ch330-nh.json \
  --version 1.0.0 \
  --dry-run
```

To test an actual publication, set `ZENODO_ACCESS_TOKEN` to the Sandbox token
and run with `--sandbox` plus a temporary registry path:

```bash
ZENODO_ACCESS_TOKEN="$ZENODO_SANDBOX_ACCESS_TOKEN" \
python scripts/publish_zenodo_directory.py \
  --path tropical_cyclones/model_simulations/HadGEM3/TRACK/u-ch330/NH \
  --metadata-file zenodo/metadata/tropical-cyclones-hadgem3-track-u-ch330-nh.json \
  --version 1.0.0 \
  --sandbox \
  --registry /tmp/track-data-zenodo-sandbox-records.json \
  --no-readme
```

## Important operational notes

- The script publishes **datasets**, not software packages. That matches the
  data-oriented purpose of `tracks`.
- A GitHub Actions run checks out the commit that triggered the workflow. Commit
  changes in the selected directory before publishing.
- If the directory contains Git LFS files, the supplied workflow checks them
  out with `lfs: true` so that the ZIP contains actual file content, not LFS
  pointer text.
- Zenodo's current bucket API documentation describes a 50 GiB maximum per
  file. This publisher creates one ZIP file per selected directory and checks
  that limit before upload. Large datasets should be tested in Sandbox first.
- Publishing is irreversible in the sense that a published Zenodo version
  cannot be deleted through the normal deposit API. Test first in Sandbox.
- If an upload fails after Zenodo creates a draft, inspect **My uploads** in
  Zenodo before re-running. The published version is not affected by an
  unpublished draft, but a pending draft may need to be resumed or discarded.

## README DOI table

The script creates or replaces the section between these markers in the root
README (production only):

```markdown
<!-- ZENODO-DOI-TABLE:START -->
...
<!-- ZENODO-DOI-TABLE:END -->
```

The table is generated from `zenodo/records.json`; avoid editing the generated
section manually.
