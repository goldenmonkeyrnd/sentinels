"""Versioned, strict contracts shared by the CLI, analyzer, and browser worker."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status: TypeAlias = Literal["PASS", "FAIL", "UNRESOLVED", "ERROR"]
ContractKind: TypeAlias = Literal["connection", "handoff", "shape"]
JSONType: TypeAlias = Literal["string", "integer", "number", "boolean", "object", "array", "null", "any"]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_default=True)

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        # JSON normally keeps the last duplicate key, which loses input identity.
        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        json.loads(json_data, object_pairs_hook=unique_object)
        return super().model_validate_json(json_data, **kwargs)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


class FieldSpec(StrictModel):
    type: JSONType = "any"
    required: bool = True
    nullable: bool = False


class ConnectionContract(StrictModel):
    kind: Literal["connection"] = "connection"
    id: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    target: str = Field(min_length=1)


class HandoffContract(StrictModel):
    kind: Literal["handoff"] = "handoff"
    id: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    consumer: str | None = None
    argument: str | int | None = None
    field: str | None = None
    returned_field: str | None = None

    @model_validator(mode="after")
    def valid_destination(self) -> HandoffContract:
        call_destination = self.consumer is not None or self.argument is not None
        if call_destination and (not self.consumer or self.argument is None):
            raise ValueError("a consumer handoff requires both consumer and argument")
        if call_destination == (self.returned_field is not None):
            raise ValueError("choose either consumer with argument, or returned_field")
        if isinstance(self.argument, int) and self.argument < 0:
            raise ValueError("positional argument must be nonnegative")
        for name in ("consumer", "argument", "field", "returned_field"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{name} must not be empty")
        return self


class ShapeContract(StrictModel):
    kind: Literal["shape"] = "shape"
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    mode: Literal["mapping", "exports", "artifact", "model"] = "mapping"
    fields: dict[str, FieldSpec] = Field(min_length=1)
    allow_extra: bool = False


Contract: TypeAlias = Annotated[
    ConnectionContract | HandoffContract | ShapeContract,
    Field(discriminator="kind"),
]


class Config(StrictModel):
    version: Literal[1] = 1
    source_roots: list[str] = Field(default_factory=lambda: ["."])
    excludes: list[str] = Field(default_factory=lambda: ["tests/**", "test_*.py"])
    max_nodes: int = Field(default=20_000, ge=1, le=1_000_000)
    max_steps: int = Field(default=10_000, ge=1, le=1_000_000)
    contracts: list[Contract] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_contracts(self) -> Config:
        identities: set[str] = set()
        for contract in self.contracts:
            if not contract.id.strip():
                raise ValueError("contract identities must not be blank")
            if contract.id in identities:
                raise ValueError(f"duplicate contract identity: {contract.id}")
            identities.add(contract.id)
        if not self.source_roots:
            raise ValueError("at least one source root is required")
        for path in self.source_roots:
            if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or "\\" in path:
                raise ValueError("source roots must be relative paths within the project")
        return self


class SourceLocation(StrictModel):
    path: str
    line: int = Field(ge=1)
    column: int = Field(default=0, ge=0)


class GraphEdge(StrictModel):
    source: str
    target: str
    kind: str
    location: SourceLocation
    detail: str = ""


class Finding(StrictModel):
    code: str
    message: str
    location: SourceLocation | None = None


class ContractResult(StrictModel):
    id: str
    kind: ContractKind
    status: Status
    findings: list[Finding] = Field(default_factory=list)
    witness: list[GraphEdge] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class AnalysisReport(StrictModel):
    version: Literal[1] = 1
    checker_version: str
    parser_version: str
    source_hash: str
    config_hash: str
    status: Status
    scope: list[str] = Field(default_factory=list)
    contracts: list[ContractResult] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    errors: list[Finding] = Field(default_factory=list)


class Bundle(StrictModel):
    version: Literal[1] = 1
    files: dict[str, str]
    config: Config

    @model_validator(mode="after")
    def relative_files(self) -> Bundle:
        normalized: set[str] = set()
        for path in self.files:
            pure = PurePosixPath(path)
            if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or pure.as_posix() == ".":
                raise ValueError("bundle paths must name files within the project")
            key = pure.as_posix()
            if key in normalized:
                raise ValueError(f"duplicate normalized source identity: {key}")
            normalized.add(key)
        return self


# The browser bridge names the same versioned model explicitly.
ProjectBundle = Bundle


def validate_config(value: object) -> Config:
    """Use the same strict JSON boundary for native TOML and browser input."""
    if isinstance(value, Config):
        value = value.model_dump(mode="json")
    return Config.model_validate_json(json.dumps(value, allow_nan=False))


def validate_bundle(value: object) -> Bundle:
    if isinstance(value, Bundle):
        value = value.model_dump(mode="json")
    return Bundle.model_validate_json(json.dumps(value, allow_nan=False))
