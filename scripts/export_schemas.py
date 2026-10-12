"""Export the public contracts without importing or analyzing applications."""

from pathlib import Path
import json

from sentinels.models import AnalysisReport, Bundle, Config


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "docs" / "schemas"
    destination.mkdir(parents=True, exist_ok=True)
    for name, model in [("config", Config), ("bundle", Bundle), ("report", AnalysisReport)]:
        (destination / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
