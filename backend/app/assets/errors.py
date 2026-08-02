class AssetValidationError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class AssetServiceError(RuntimeError):
    code = "asset_service_error"
    http_status = 500


class AssetNotFoundError(AssetServiceError):
    code = "asset_not_found"
    http_status = 404


class AssetExpiredError(AssetServiceError):
    code = "asset_expired"
    http_status = 404


class AssetNotReadyError(AssetServiceError):
    code = "asset_not_ready"
    http_status = 409


class AssetAnalysisFailedError(AssetServiceError):
    code = "asset_analysis_failed"
    http_status = 409


class AssetPersistenceError(AssetServiceError):
    code = "asset_persistence_failed"
    http_status = 503
