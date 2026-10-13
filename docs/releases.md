# Publishing and promoting releases

`goldenmonkeyrnd/sentinels` owns the analyzer and lab. CivicAtlas and the portfolio consume exact qualified artifacts. PyPI publication remains outside this increment.

## Publish a release

Update `pyproject.toml`, `src/sentinels/__init__.py`, and `package.json` together, refresh `uv.lock`, regenerate schemas and examples when contracts change, and commit the source. Push a stable `vMAJOR.MINOR.PATCH` tag for that source revision. `.github/workflows/release.yml` builds the wheel and source distribution once. Three qualification jobs import that same wheel on Python 3.12–3.14, run all tests with pinned native adapters and zero skips, and exercise the actual CLI against broken and repaired examples. The browser job uses the same wheel and the bounded native fixture corpus in actual Pyodide.

Qualification checks semantic findings and witnesses, stale results, cancellation/recovery, keyboard/mobile use, input limits, offline analysis, source privacy, and zero runtime loading before activation. Unsupported analysis remains explicit. The fixed executed counterexample remains separate from analysis of edited source.

The publisher binds the source SHA, successful qualification job identities, wheel/archive digests, notices, and byte sizes in `release-qualification.json`. It aggregates interpreter results in `native-qualification.json`, preserves `browser-qualification.json`, and publishes the wheel, source distribution, browser archive, corresponding Git source, inventory, full AGPL license, attribution, third-party notices, and `SHA256SUMS`. It publishes through a draft; repository release immutability locks the finished assets. Qualification failure prevents publication. A retry verifies an existing release rather than replacing its assets.

## Promote the website

The publisher uses the **GoldenMonkey Sentinels Release** GitHub App to dispatch `promote-sentinels.yml` in `goldenmonkeyrnd/golden-monkey-rnd`. The App is installed only on that repository, with Actions write and required Metadata read access. Its ID is the Sentinels Actions variable `SENTINELS_PROMOTION_APP_ID`; its private key is the encrypted secret `SENTINELS_PROMOTION_APP_PRIVATE_KEY`.

The website verifies the source tag, qualification jobs, record and asset digests, native/browser wheel identity, clean source provenance, and safe archive inventory. It qualifies a candidate before committing its selected release pin and catalog, then deploys the exact checked site artifact. The App does not have repository Contents or Secrets permissions. Website commits use the website workflow's own token.

The public address stays **https://goldenmonkey.dev/labs/sentinels/**. Hugo supplies shared navigation, breadcrumbs, and footer. Workspaces live under immutable archive-digest paths; the release catalog retains deployed bundles so open sessions keep their original assets. The old versioned landing address redirects while subordinate asset routes remain available. Article embeds load the workspace directly.

A rollback selects a retained digest and records the rejected release so automatic promotion cannot immediately restore it. Never replace assets beneath an existing digest. Apply worker/Wasm policy only to workspace routes. Keep Python loading behind **Try the lab**, with no lab telemetry or automatic edit persistence.

## Adopt and accept

CivicAtlas commits a hashed released-wheel URL and dependency lock, uses `uv sync --locked`, and retains its runtime data validation and reproducibility gates. Coverage adoption reviews every selected production declaration rather than automatically accepting a regenerated ledger.

Production acceptance requires actual visitor-generated findings, repair, downloads, navigation, offline analysis after initialization, and worker recovery. Historical recordings remain evidence with their original provenance; screenshots alone do not qualify the analyzer or deployment.
