# Release v0.1.0

The release source of truth is `goldenmonkeyrnd/sentinels`. CivicAtlas and the
portfolio consume an exact qualified release; they do not keep separate analyzer
implementations. PyPI publication is outside this release.

## Qualify the artifacts

Run the native tests on Python 3.12, 3.13, and 3.14, including meaningful fault
injection and import-side-effect fixtures. Exercise the Prefect 3.6.11 and Typer
0.20.0 adapter fixtures natively without external services. Build the wheel and
source distribution, export schemas, and build the lab using its pinned runtime.

Run the same fixture bundles in actual browser Pyodide. Compare semantic findings
and witnesses after normalizing interpreter provenance. Check stale result
handling, watchdog termination/recovery, keyboard and mobile access, input bounds,
offline analysis after loading, and absence of source/report uploads. Check that
there are no runtime downloads before activation and that each static asset fits
the deployment platform's size limits.

Release assets must include the Python wheel, source distribution, browser
archive, corresponding source archive, dependency notices, artifact inventory,
and SHA-256 checksums. Verify the checksums against the finished assets before
creating the tag and public GitHub release. Preserve attribution and the full
AGPL license in the source and distributions. The browser must provide its exact
source/release links and license notices.

## Adopt and deploy

After publication, CivicAtlas pins the GitHub wheel URL and hash in its committed
dependency lock and uses `uv sync --locked`. Verify its real validator connection,
manifest handoff, and snapshot shape, while retaining runtime data validation and
reproducibility gates. Retire the duplicated checker and generic fixtures after
the released checker satisfies the real integration checks.

The portfolio pins the lab archive's URL and checksum. Its importer rejects
absolute/traversal paths, links, and unexpected archive content and stages assets
locally at `/labs/sentinels/v0.1.0/`. Verify the produced lab headers and lazy
activation before deploying. Keep historical recordings and their original
provenance; retire their old generator when the live lab lands.

Production acceptance requires a visitor to edit an example, obtain a fresh
source-linked result, repair it, and download its bundle/report from the deployed
site. A screenshot or recording alone does not complete that acceptance.
