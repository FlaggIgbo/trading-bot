"""
Tests for Cloudflare R2 and MinIO Object Storage Layer
"""

import os
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from storage import (
    LogStorageManager,
    PurePythonS3Client,
    StorageConfig,
    get_default_config,
)


def test_get_default_config_minio_dev(monkeypatch):
    monkeypatch.setenv("LOG_STORAGE_BACKEND", "auto")
    monkeypatch.setenv("KALSHI_USE_DEMO", "true")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MINIO_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_BUCKET_NAME", "trading-bot-logs-dev")

    config = get_default_config(env="demo")
    assert config.backend == "minio"
    assert config.endpoint_url == "http://localhost:9000"
    assert config.access_key_id == "minioadmin"
    assert config.secret_access_key == "minioadmin"
    assert config.bucket_name == "trading-bot-logs-dev"


def test_get_default_config_r2_prod(monkeypatch):
    monkeypatch.setenv("LOG_STORAGE_BACKEND", "auto")
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123456")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "r2_key_xyz")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "r2_sec_xyz")
    monkeypatch.setenv("R2_BUCKET_NAME", "trading-bot-logs-prod")

    config = get_default_config(env="prod")
    assert config.backend == "r2"
    assert "abc123456.r2.cloudflarestorage.com" in config.endpoint_url
    assert config.access_key_id == "r2_key_xyz"
    assert config.secret_access_key == "r2_sec_xyz"
    assert config.bucket_name == "trading-bot-logs-prod"


def test_get_default_config_explicit_override(monkeypatch):
    monkeypatch.setenv("MINIO_ENDPOINT_URL", "http://127.0.0.1:9000")
    config = get_default_config(backend_override="local")
    assert config.backend == "local"


def test_pure_python_s3_auth_headers():
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://localhost:9000",
        access_key_id="testuser",
        secret_access_key="testpass",
        bucket_name="testbucket",
    )
    client = PurePythonS3Client(config)
    headers = client._build_auth_headers(
        method="PUT",
        uri_path="/testbucket/test.log",
        payload=b"test data",
    )
    assert "Authorization" in headers
    assert "AWS4-HMAC-SHA256" in headers["Authorization"]
    assert "Credential=testuser/" in headers["Authorization"]
    assert "x-amz-content-sha256" in headers
    assert "x-amz-date" in headers


def test_pure_python_s3_put_and_get():
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://localhost:9000",
        access_key_id="testuser",
        secret_access_key="testpass",
        bucket_name="testbucket",
    )
    client = PurePythonS3Client(config)

    mock_resp_put = MagicMock()
    mock_resp_put.status_code = 200

    mock_resp_get = MagicMock()
    mock_resp_get.status_code = 200
    mock_resp_get.content = b"sample log contents"

    with patch("requests.put", return_value=mock_resp_put):
        ok = client.put_object("run.log", b"sample log contents", "text/plain")
        assert ok is True

    with patch("requests.get", return_value=mock_resp_get):
        data = client.get_object("run.log")
        assert data == b"sample log contents"


def test_pure_python_s3_list_objects():
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://localhost:9000",
        access_key_id="testuser",
        secret_access_key="testpass",
        bucket_name="testbucket",
    )
    client = PurePythonS3Client(config)

    sample_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
        <Name>testbucket</Name>
        <Contents>
            <Key>runs/20261117/parallel_run.log</Key>
            <LastModified>2026-11-17T20:00:00.000Z</LastModified>
            <Size>12345</Size>
        </Contents>
    </ListBucketResult>"""

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = sample_xml

    with patch("requests.get", return_value=mock_resp):
        items = client.list_objects(prefix="runs/")
        assert len(items) == 1
        assert items[0]["Key"] == "runs/20261117/parallel_run.log"
        assert items[0]["Size"] == 12345


def test_storage_manager_graceful_offline():
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://127.0.0.1:9999",  # Non-existent port
        access_key_id="minioadmin",
        secret_access_key="minioadmin",
        bucket_name="testbucket",
    )
    manager = LogStorageManager(config=config)
    ok, msg = manager.test_connection()
    assert ok is False
    assert "Failed to connect" in msg or "returned non-200" in msg or "Unreachable" in msg


def test_storage_manager_upload_and_download_file(tmp_path):
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://localhost:9000",
        access_key_id="minioadmin",
        secret_access_key="minioadmin",
        bucket_name="testbucket",
    )
    manager = LogStorageManager(config=config)

    # Create dummy local file
    test_file = tmp_path / "sample_run.log"
    test_file.write_text("2026-11-17 [INFO] Order placed")

    with patch.object(manager, "upload_bytes", return_value=True):
        res = manager.upload_file(test_file, remote_key="runs/sample_run.log")
        assert res is True

    download_target = tmp_path / "downloaded.log"
    with patch.object(manager, "download_bytes", return_value=b"2026-11-17 [INFO] Order placed"):
        dl_res = manager.download_file("runs/sample_run.log", download_target)
        assert dl_res is True
        assert download_target.read_text() == "2026-11-17 [INFO] Order placed"


def test_storage_manager_upload_run_logs(tmp_path):
    config = StorageConfig(
        backend="minio",
        endpoint_url="http://localhost:9000",
        access_key_id="minioadmin",
        secret_access_key="minioadmin",
        bucket_name="testbucket",
    )
    manager = LogStorageManager(config=config)

    f1 = tmp_path / "run.log"
    f2 = tmp_path / "run.db"
    f1.write_text("log data")
    f2.write_text("db data")

    with patch.object(manager, "upload_file", return_value=True):
        uploaded = manager.upload_run_logs("test_run", files=[f1, f2], date_prefix=False)
        assert len(uploaded) == 2
        assert "runs/test_run/run.log" in uploaded
        assert "runs/test_run/run.db" in uploaded
