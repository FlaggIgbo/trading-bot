"""
Storage Interface for Trading Bot
=================================
Exposes S3-compatible log storage functions for Cloudflare R2 (Prod) and MinIO (Dev).
"""

from logging.storage import (
    LogStorageManager,
    PurePythonS3Client,
    StorageConfig,
    get_default_config,
    storage_manager,
)

__all__ = [
    "LogStorageManager",
    "PurePythonS3Client",
    "StorageConfig",
    "get_default_config",
    "storage_manager",
]
