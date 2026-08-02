from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OcrSettings(BaseSettings):
    api_key: str = ""
    model_name: str = "PP-OCRv5_mobile"
    model_dir: str = "/models"
    device: str = "cpu"
    cpu_threads: int = Field(default=4, ge=1, le=64)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_image_pixels: int = Field(default=20_000_000, ge=1)

    model_config = SettingsConfigDict(env_prefix="OCR_")


settings = OcrSettings()
