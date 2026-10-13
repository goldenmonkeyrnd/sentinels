# Sentinels

Sentinels checks declared integration requirements in Python source. It reports
where a production entry connects to a validator, where a particular result is
handed to a consumer, whether a published shape matches its contract, and which production declarations still need a coverage decision. Each
result includes its inspected scope, source locations, and available witness.

**A structural connection does not establish successful execution.** A branch
can bypass a connected validator. Runtime correctness still needs appropriate
tests and observations. Sentinels never imports the analyzed application,
executes its decorators, evaluates its annotations, or instantiates its models.

This is the source of the reusable tool and editable browser lab displayed at
[Golden Monkey R&D](https://goldenmonkey.dev/labs/sentinels/).
The repository is licensed **AGPL-3.0-only**; dependencies retain their own
licenses. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Run locally

Use Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/). Clone the repository:

```sh
git clone https://github.com/goldenmonkeyrnd/sentinels.git
cd sentinels
uv sync --locked
uv run sentinels --help
uv run sentinels check --bundle examples/connection-repaired.json --json
```

Create `sentinels.toml` in the application you want to inspect:

```toml
version = 2
source_roots = ["."]
excludes = ["tests/**", "test_*.py"]

[[contracts]]
kind = "connection"
id = "validate-before-publication"
entry = "pipeline.build:main"
target = "pipeline.validate:validate_snapshot"
```

Run the installed checker against that application:

```sh
uv run sentinels check --root /path/to/application --config sentinels.toml
uv run sentinels check --root /path/to/application --config sentinels.toml --json
```

The config path is relative to `--root` unless absolute. Inspection selects
Python files beneath `source_roots`, excludes the configured globs, and reads
only explicitly selected JSON artifacts for artifact contracts. Use qualified
symbols such as `package.module:function` or `package.module:Class.method`.
An entry is an explicit assertion about production ownership; decorators and
tests alone do not make a function a production entry.

The exit codes are `0 PASS`, `1 FAIL`, `2 UNRESOLVED`, and `3 ERROR`.
Reports preserve individual violations and unresolved requirements even when
the aggregate status has a different precedence. Invalid configuration or
selected inputs are errors. Unsupported code is relevant to a result only when
it prevents resolution of that requirement.

## Declare more requirements

Track the value returned by a producer into a consumer's named argument:

```toml
[[contracts]]
kind = "handoff"
id = "manifest-to-observations"
entry = "pipeline.build:build"
producer = "pipeline.build:load_manifest"
consumer = "pipeline.build:build_observations"
argument = "manifest"
```

Use `field = "name"` to require a selected producer field. A handoff can instead
declare `returned_field = "name"` to require that value in the entry's returned
payload. A discarded result, overwritten binding, or unrelated same-named key
does not satisfy the requirement.

For a committed JSON artifact, declare fields and their JSON types:

```toml
[[contracts]]
kind = "shape"
id = "snapshot-shape"
mode = "artifact"
source = "data/processed/snapshot.json"
allow_extra = false

[contracts.fields.dataset_revision]
type = "string"

[contracts.fields.metric_definitions]
type = "object"
```

The other shape modes are `mapping` (return mappings), `exports` (literal
`__all__`), and `model` (source-declared Pydantic fields). Supported field types
are `string`, `integer`, `number`, `boolean`, `object`, `array`, `null`, and
`any`. `required` defaults to true and `nullable` to false. Optional presence and
nullable values are different requirements. A model annotation alone does not
prove runtime validation happened. Each return shape is checked separately.

Coverage discovers every declaration in selected production directories, including private helpers and declarations outside reachable call paths. Each surface needs a meaningful contract association or an explicit internal classification:

```toml
[[contracts]]
kind = "coverage"
id = "pipeline-coverage"
roots = ["pipeline"]

[[contracts.ledger]]
kind = "contract"
surface = "pipeline.build:main"
contracts = ["validate-before-publication"]

[[contracts.ledger]]
kind = "internal"
surface = "pipeline.build:_format_label"
reason = "Formats a display label; it does not own an integration requirement."
```

This is a partial ledger: review and declare a role for every discovered surface. A new unclassified declaration or a stale identity fails coverage. An arbitrary contract ID cannot justify a surface; invalid associations are configuration errors. Internal classification records intent and does not verify behavior. Relevant discovery limitations remain unresolved. Native and browser configuration, bundles, reports, and exported schemas use version 2. Reproduce historical bundles with their original released checker.

See [analysis semantics](docs/semantics.md), the [versioned JSON schemas](docs/schemas/),
and [browser lab operations](docs/browser-lab.md).

## Reproduce a browser result

Download a project bundle from the lab and run:

```sh
uv run sentinels check --bundle project.json --json
```

The bundle contains `version`, a `files` mapping of relative paths to source or
selected JSON artifact text, and the same `config` accepted by the native CLI.
Edited application source remains data; it is never run. The report contains
source/configuration hashes so you can identify the inputs behind a result.

The Python API has the same boundary:

```python
from sentinels import Config, analyze_project, analyze_sources

config = Config.model_validate_json(config_json)
report = analyze_sources({"app.py": source_text}, config)
report = analyze_project("/path/to/application", config)
```

## Develop and verify

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python scripts/export_schemas.py
uv build
```

Framework integration fixtures use the optional pinned native dependencies:

```sh
uv sync --locked --extra adapters
uv run --locked --extra adapters python -m unittest discover -s tests -v
```

The adapter packages are used for verification; source analysis does not import
them. Browser builds use the same pure Python wheel with a pinned Pyodide runtime
and its compiled Pydantic dependency closure. Build and deployment details live
with the [release process](docs/releases.md). PyPI publication is outside v0.1.0.

The original Babylon tooling and CivicAtlas recorded examples are historical
design evidence. The standalone release is the implementation consumed by
CivicAtlas and the website.
