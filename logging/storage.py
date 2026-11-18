"""
S3-Compatible Log & Artifact Storage Manager
=============================================
Provides unified object storage for run logs, databases, and artifacts:
- Production: Cloudflare R2 (S3-compatible)
- Local Development: MinIO (running via Docker at localhost:9000)
- Graceful Fallback: Local filesystem caching when offline or unconfigured

Supports both `boto3` (when installed) and a pure-Python SigV4 client using `requests`.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

try:
    import boto3
    from botocore.client import Config as BotoConfig
    from botocore.exceptions import ClientError as BotoClientError
    _HAS_BOTO3 = True
except ImportError:
    boto3 = None
    BotoConfig = None
    BotoClientError = None
    _HAS_BOTO3 = False

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    requests = None
    _HAS_REQUESTS = False


@dataclass
class StorageConfig:
    backend: str  # "r2", "minio", "local"
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    region_name: str = "auto"
    use_path_style: bool = True


def get_default_config(
    env: Optional[str] = None,
    backend_override: Optional[str] = None,
) -> StorageConfig:
    """Resolve storage configuration based on environment variables."""
    backend_choice = (
        backend_override
        or os.getenv("LOG_STORAGE_BACKEND")
        or "auto"
    ).lower().strip()

    is_prod = False
    if env:
        is_prod = str(env).lower() in ("prod", "production")
    else:
        kalshi_demo = os.getenv("KALSHI_USE_DEMO", "true").lower() in ("true", "1", "yes")
        app_env = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).lower()
        is_prod = (not kalshi_demo) or (app_env in ("prod", "production"))

    # Explicit or auto selection
    if backend_choice == "r2" or (backend_choice == "auto" and is_prod):
        # Cloudflare R2 Production Config
        account_id = os.getenv("R2_ACCOUNT_ID") or os.getenv("CLOUDFLARE_R2_ACCOUNT_ID", "")
        default_endpoint = f"https://{account_id}.r2.cloudflarestorage.com" if account_id else ""
        endpoint = os.getenv("R2_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or default_endpoint
        if account_id and endpoint and "<your_account_id>" in endpoint:
            endpoint = endpoint.replace("<your_account_id>", account_id)
        if account_id and endpoint and "<account_id>" in endpoint:
            endpoint = endpoint.replace("<account_id>", account_id)
        access_key = os.getenv("R2_ACCESS_KEY_ID") or os.getenv("S3_ACCESS_KEY_ID", "")
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("S3_SECRET_ACCESS_KEY", "")
        bucket = os.getenv("R2_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME", "trading-bot-logs-prod")
        return StorageConfig(
            backend="r2",
            endpoint_url=endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
            bucket_name=bucket,
            region_name=os.getenv("R2_REGION", os.getenv("S3_REGION", "auto")),
            use_path_style=True,
        )
    elif backend_choice == "minio" or (backend_choice == "auto" and not is_prod):
        # MinIO Local Development Config
        endpoint = (
            os.getenv("MINIO_ENDPOINT_URL")
            or os.getenv("S3_ENDPOINT_URL")
            or "http://localhost:9000"
        )
        access_key = (
            os.getenv("MINIO_ACCESS_KEY")
            or os.getenv("MINIO_ROOT_USER")
            or os.getenv("S3_ACCESS_KEY_ID")
            or "minioadmin"
        )
        secret_key = (
            os.getenv("MINIO_SECRET_KEY")
            or os.getenv("MINIO_ROOT_PASSWORD")
            or os.getenv("S3_SECRET_ACCESS_KEY")
            or "minioadmin"
        )
        bucket = (
            os.getenv("MINIO_BUCKET_NAME")
            or os.getenv("S3_BUCKET_NAME")
            or "trading-bot-logs-dev"
        )
        return StorageConfig(
            backend="minio",
            endpoint_url=endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
            bucket_name=bucket,
            region_name=os.getenv("MINIO_REGION", os.getenv("S3_REGION", "us-east-1")),
            use_path_style=True,
        )
    else:
        # Local fallback only
        return StorageConfig(
            backend="local",
            endpoint_url="",
            access_key_id="",
            secret_access_key="",
            bucket_name="local-logs",
            region_name="local",
            use_path_style=True,
        )


class PurePythonS3Client:
    """Lightweight AWS Signature Version 4 S3 Client using requests (no boto3 required)."""

    def __init__(self, config: StorageConfig):
        self.config = config
        self.endpoint_url = config.endpoint_url.rstrip("/")
        self.access_key = config.access_key_id
        self.secret_key = config.secret_access_key
        self.bucket = config.bucket_name
        self.region = config.region_name or "us-east-1"

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _get_signature_key(self, date_stamp: str) -> bytes:
        k_date = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region = self._sign(k_date, self.region)
        k_service = self._sign(k_region, "s3")
        k_signing = self._sign(k_service, "aws4_request")
        return k_signing

    def _build_auth_headers(
        self,
        method: str,
        uri_path: str,
        query_string: str = "",
        payload: bytes = b"",
        headers_extra: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        payload_hash = hashlib.sha256(payload).hexdigest()

        parsed = urllib.parse.urlparse(self.endpoint_url)
        host = parsed.netloc

        headers = {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        if headers_extra:
            for k, v in headers_extra.items():
                headers[k.lower()] = v

        canonical_headers = "".join(f"{k.lower()}:{headers[k]}\n" for k in sorted(headers))
        signed_headers = ";".join(sorted(k.lower() for k in headers))

        canonical_request = (
            f"{method}\n"
            f"{uri_path}\n"
            f"{query_string}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = self._get_signature_key(date_stamp)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"{algorithm} Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        headers["Authorization"] = auth_header
        return headers

    def put_object(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> bool:
        if not _HAS_REQUESTS or not self.endpoint_url:
            return False
        clean_key = key.lstrip("/")
        uri_path = f"/{self.bucket}/{clean_key}"
        url = f"{self.endpoint_url}{uri_path}"
        headers = self._build_auth_headers(
            method="PUT",
            uri_path=uri_path,
            payload=body,
            headers_extra={"Content-Type": content_type},
        )
        headers["Content-Type"] = content_type
        resp = requests.put(url, data=body, headers=headers, timeout=15)
        return resp.status_code in (200, 204)

    def get_object(self, key: str) -> Optional[bytes]:
        if not _HAS_REQUESTS or not self.endpoint_url:
            return None
        clean_key = key.lstrip("/")
        uri_path = f"/{self.bucket}/{clean_key}"
        url = f"{self.endpoint_url}{uri_path}"
        headers = self._build_auth_headers(method="GET", uri_path=uri_path)
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.content
        return None

    def head_bucket(self) -> bool:
        if not _HAS_REQUESTS or not self.endpoint_url:
            return False
        uri_path = f"/{self.bucket}"
        url = f"{self.endpoint_url}{uri_path}"
        headers = self._build_auth_headers(method="HEAD", uri_path=uri_path)
        try:
            resp = requests.head(url, headers=headers, timeout=5)
            return resp.status_code in (200, 204)
        except Exception:
            return False

    def list_objects(self, prefix: str = "") -> list[dict]:
        if not _HAS_REQUESTS or not self.endpoint_url:
            return []
        uri_path = f"/{self.bucket}"
        query = "list-type=2"
        if prefix:
            query += f"&prefix={urllib.parse.quote(prefix)}"
        url = f"{self.endpoint_url}{uri_path}?{query}"
        headers = self._build_auth_headers(method="GET", uri_path=uri_path, query_string=query)
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return []
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)
            items = []
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            contents = root.findall("s3:Contents", ns) or root.findall("Contents")
            for node in contents:
                k = node.findtext("s3:Key", namespaces=ns) or node.findtext("Key") or ""
                size = node.findtext("s3:Size", namespaces=ns) or node.findtext("Size") or "0"
                last_mod = node.findtext("s3:LastModified", namespaces=ns) or node.findtext("LastModified") or ""
                items.append({"Key": k, "Size": int(size), "LastModified": last_mod})
            return items
        except Exception:
            return []


class LogStorageManager:
    """Manages uploading and downloading run logs to Cloudflare R2 or MinIO."""

    def __init__(
        self,
        config: Optional[StorageConfig] = None,
        env: Optional[str] = None,
        backend_override: Optional[str] = None,
    ):
        self.config = config or get_default_config(env=env, backend_override=backend_override)
        self._boto_client = None
        self._pure_client = None

        if self.config.backend != "local" and self.config.endpoint_url:
            if _HAS_BOTO3:
                try:
                    self._boto_client = boto3.client(
                        "s3",
                        endpoint_url=self.config.endpoint_url,
                        aws_access_key_id=self.config.access_key_id,
                        aws_secret_access_key=self.config.secret_access_key,
                        region_name=self.config.region_name,
                        config=BotoConfig(
                            s3={"addressing_style": "path" if self.config.use_path_style else "auto"},
                            signature_version="s3v4",
                        ),
                    )
                except Exception:
                    self._boto_client = None

            # Always initialize lightweight pure-Python SigV4 client as alternative/fallback
            self._pure_client = PurePythonS3Client(self.config)

    @property
    def is_configured(self) -> bool:
        return (
            self.config.backend != "local"
            and bool(self.config.endpoint_url)
            and bool(self.config.bucket_name)
        )

    def test_connection(self) -> tuple[bool, str]:
        """Test whether the object storage backend is reachable and bucket accessible."""
        if not self.is_configured:
            return False, f"Storage backend is '{self.config.backend}' or missing endpoint URL."

        if self._boto_client:
            try:
                self._boto_client.head_bucket(Bucket=self.config.bucket_name)
                return True, f"Connected to {self.config.backend.upper()} bucket '{self.config.bucket_name}' via boto3."
            except Exception as e:
                # Fall back to testing pure client
                pass

        if self._pure_client:
            try:
                ok = self._pure_client.head_bucket()
                if ok:
                    return True, f"Connected to {self.config.backend.upper()} bucket '{self.config.bucket_name}' via HTTP SigV4."
                return False, f"Bucket '{self.config.bucket_name}' returned non-200 status on {self.config.endpoint_url}."
            except Exception as e:
                return False, f"Failed to connect to {self.config.endpoint_url}: {e}"

        return False, "No S3 client available."

    def upload_bytes(
        self,
        data: bytes,
        remote_key: str,
        content_type: Optional[str] = None,
    ) -> bool:
        """Upload in-memory bytes to object storage."""
        if not self.is_configured:
            return False

        if content_type is None:
            content_type, _ = mimetypes.guess_type(remote_key)
            content_type = content_type or "application/octet-stream"

        if self._boto_client:
            try:
                self._boto_client.put_object(
                    Bucket=self.config.bucket_name,
                    Key=remote_key,
                    Body=data,
                    ContentType=content_type,
                )
                return True
            except Exception:
                pass

        if self._pure_client:
            try:
                return self._pure_client.put_object(
                    key=remote_key,
                    body=data,
                    content_type=content_type,
                )
            except Exception:
                pass

        return False

    def upload_file(
        self,
        local_path: str | Path,
        remote_key: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> bool:
        """Upload a local file to object storage."""
        path = Path(local_path)
        if not path.is_file():
            return False

        key = remote_key or path.name

        try:
            with open(path, "rb") as f:
                data = f.read()
            return self.upload_bytes(data, key, content_type=content_type)
        except Exception:
            return False

    def download_bytes(self, remote_key: str) -> Optional[bytes]:
        """Download raw bytes from object storage."""
        if not self.is_configured:
            return None

        if self._boto_client:
            try:
                res = self._boto_client.get_object(
                    Bucket=self.config.bucket_name,
                    Key=remote_key,
                )
                return res["Body"].read()
            except Exception:
                pass

        if self._pure_client:
            try:
                return self._pure_client.get_object(key=remote_key)
            except Exception:
                pass

        return None

    def download_file(self, remote_key: str, local_path: str | Path) -> bool:
        """Download remote object and save to local disk."""
        data = self.download_bytes(remote_key)
        if data is None:
            return False

        out_path = Path(local_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(out_path, "wb") as f:
                f.write(data)
            return True
        except Exception:
            return False

    def list_logs(self, prefix: str = "") -> list[dict]:
        """List remote log objects."""
        if not self.is_configured:
            return []

        if self._boto_client:
            try:
                res = self._boto_client.list_objects_v2(
                    Bucket=self.config.bucket_name,
                    Prefix=prefix,
                )
                items = []
                for obj in res.get("Contents", []):
                    items.append({
                        "Key": obj["Key"],
                        "Size": obj["Size"],
                        "LastModified": str(obj["LastModified"]),
                    })
                return items
            except Exception:
                pass

        if self._pure_client:
            return self._pure_client.list_objects(prefix=prefix)

        return []

    def upload_run_logs(
        self,
        run_tag: str,
        files: Optional[list[str | Path]] = None,
        date_prefix: bool = True,
    ) -> list[str]:
        """Upload all log and db files associated with a run."""
        if not self.is_configured:
            return []

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"runs/{date_str}/{run_tag}" if date_prefix else f"runs/{run_tag}"

        uploaded = []
        target_files = files or []

        # If files not provided, look for root run log & db files
        if not target_files:
            root = Path(__file__).resolve().parent.parent
            for ext in ("*.log", "*.db", ".parallel_run_meta.json"):
                for p in root.glob(ext):
                    if p.name != "backtest.db":
                        target_files.append(p)

        for p in target_files:
            path = Path(p)
            if not path.is_file():
                continue
            remote_key = f"{prefix}/{path.name}"
            success = self.upload_file(path, remote_key=remote_key)
            if success:
                uploaded.append(remote_key)

        return uploaded


# Global instance
storage_manager = LogStorageManager()
