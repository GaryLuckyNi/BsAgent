from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FinancialMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=128)
    unit: str | None = Field(default=None, max_length=64)
    period: str | None = Field(default=None, max_length=64)
    source_text: str | None = Field(default=None, max_length=512)


class VisionAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    document_type: Literal[
        "financial_statement",
        "chart",
        "announcement",
        "research_report",
        "table",
        "screenshot",
        "other",
    ]
    summary: str = Field(min_length=1, max_length=4000)
    extracted_text: str = Field(default="", max_length=30000)
    financial_metrics: list[FinancialMetric] = Field(default_factory=list, max_length=100)
    chart_observations: list[str] = Field(default_factory=list, max_length=100)
    entities: list[str] = Field(default_factory=list, max_length=100)
    risk_flags: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)


class VisionAnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis: VisionAnalysis
    provider: str
    model: str
    usage: dict = Field(default_factory=dict)


class OcrExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(max_length=200000)
    tables: list[dict] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    provider: str = "paddleocr_http"
    provider_version: str


class CoordinatedImageAnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "partial"]
    analysis: VisionAnalysis
    provider: str
    model: str
    usage: dict = Field(default_factory=dict)
    ocr_status: Literal["success", "failed"]
    vision_status: Literal["success", "failed", "skipped"]
    ocr_provider: str
    ocr_version: str
    vision_provider: str
    vision_version: str
    full_ocr_text: str = Field(max_length=200000)
    ocr_tables: list[dict] = Field(default_factory=list, max_length=100)
