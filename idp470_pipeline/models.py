from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    SIGN = "sign"


class StructureScope(str, Enum):
    FILE = "file"
    INVOICE = "invoice"
    LINE = "line"


class FieldSpec(BaseModel):
    name: str = Field(min_length=1)
    start: int = Field(ge=1)
    length: int = Field(ge=1)
    type: FieldType = Field(default=FieldType.STRING)
    decimals: int | None = Field(default=None, ge=0)
    description: str | None = None

    @model_validator(mode="after")
    def validate_decimal_constraints(self) -> "FieldSpec":
        if self.type == FieldType.DECIMAL and self.decimals is None:
            raise ValueError(f"Field {self.name}: decimals is required for decimal type.")
        if self.type != FieldType.DECIMAL and self.decimals is not None:
            raise ValueError(
                f"Field {self.name}: decimals must be null when type is not decimal."
            )
        return self

    @property
    def end(self) -> int:
        return self.start + self.length - 1


class SelectorSpec(BaseModel):
    start: int = Field(ge=1)
    length: int = Field(ge=1)
    value: str = Field(min_length=1)

    @property
    def end(self) -> int:
        return self.start + self.length - 1


class RecordSpec(BaseModel):
    name: str = Field(min_length=1)
    selector: SelectorSpec
    fields: list[FieldSpec] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def validate_unique_field_names(cls, fields: list[FieldSpec]) -> list[FieldSpec]:
        names = [field.name for field in fields]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate field names: {', '.join(duplicates)}")
        return fields

    @model_validator(mode="after")
    def validate_field_positions(self) -> "RecordSpec":
        ordered = sorted(self.fields, key=lambda field: field.start)
        previous_end = 0
        for field in ordered:
            if field.start <= previous_end:
                raise ValueError(
                    f"Overlapping fields in {self.name}: {field.name} starts at {field.start} "
                    f"but previous field ends at {previous_end}."
                )
            previous_end = field.end
        return self

    @property
    def sum_of_lengths(self) -> int:
        return sum(field.length for field in self.fields)

    @property
    def max_end(self) -> int:
        return max(field.end for field in self.fields)


class StructureRule(BaseModel):
    label: str = Field(min_length=1)
    record_name: str = Field(min_length=1)
    scope: StructureScope
    min_occurs: int = Field(default=0, ge=0)
    max_occurs: int | None = Field(default=None, ge=1)
    order_index: int = Field(ge=1)
    description: str | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "StructureRule":
        if self.max_occurs is not None and self.max_occurs < self.min_occurs:
            raise ValueError(
                f"Structure rule {self.label}: max_occurs must be >= min_occurs."
            )
        return self


class ContractSpec(BaseModel):
    schema_version: str = Field(default="1.0")
    source_program: str = Field(min_length=1)
    generated_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0)
    )
    line_length: int = Field(ge=1)
    strict_length_validation: bool = True
    strict_structure_validation: bool = False
    structure_source: str | None = None
    structure_rules: list[StructureRule] = Field(default_factory=list)
    record_types: list[RecordSpec] = Field(min_length=1)

    @field_validator("record_types")
    @classmethod
    def validate_unique_record_names(cls, records: list[RecordSpec]) -> list[RecordSpec]:
        names = [record.name for record in records]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate record types: {', '.join(duplicates)}")
        return records

    @model_validator(mode="after")
    def validate_record_ranges(self) -> "ContractSpec":
        for record in self.record_types:
            if record.max_end > self.line_length:
                raise ValueError(
                    f"Record {record.name} exceeds line length {self.line_length}: "
                    f"max end position = {record.max_end}"
                )
            if self.strict_length_validation and record.sum_of_lengths != self.line_length:
                raise ValueError(
                    f"Record {record.name} has sum(lengths)={record.sum_of_lengths}, "
                    f"expected line_length={self.line_length}"
                )
        return self

    @property
    def by_name(self) -> dict[str, RecordSpec]:
        return {record.name: record for record in self.record_types}
