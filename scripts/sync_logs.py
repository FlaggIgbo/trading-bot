#!/usr/bin/env python3
"""
S3 Log Synchronization CLI (Cloudflare R2 & MinIO)
==================================================
Upload, download, list, and verify run logs between the local workspace
and object storage (Cloudflare R2 for Prod, MinIO Docker for Dev).

Examples:
    # Check connection status
    python scripts/sync_logs.py --status

    # Check connection status for production R2
    python scripts/sync_logs.py --status --env prod

    # Upload recent run logs to MinIO (dev)
    python scripts/sync_logs.py --upload-recent

    # Upload all historical archive folders
    python scripts/sync_logs.py --upload-archives

    # Upload specific files
    python scripts/sync_logs.py --upload parallel_run.log trading_bot_random_demo.db

    # List all stored logs
    python scripts/sync_logs.py --list

    # Download logs from a run
    python scripts/sync_logs.py --download-prefix runs/20261117/ --output-dir ./downloaded_logs
"""

import argparse
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv()

from storage import LogStorageManager, get_default_config


def print_status(manager: LogStorageManager):
    print("=" * 65)
    print(f"OBJECT STORAGE STATUS: {manager.config.backend.upper()}")
    print("=" * 65)
    print(f"  Backend:       {manager.config.backend}")
    print(f"  Endpoint:      {manager.config.endpoint_url or '(none)'}")
    print(f"  Bucket:        {manager.config.bucket_name}")
    print(f"  Region:        {manager.config.region_name}")
    print(f"  Access Key ID: {manager.config.access_key_id[:4]}***" if manager.config.access_key_id else "  Access Key ID: (none)")
    print("-" * 65)
    ok, msg = manager.test_connection()
    status_label = "✅ CONNECTED" if ok else "❌ UNREACHABLE"
    print(f"  Status:        {status_label}")
    print(f"  Message:       {msg}")
    print("=" * 65 + "\n")
    return ok


def list_remote_logs(manager: LogStorageManager, prefix: str = ""):
    print(f"\nListing remote logs in [{manager.config.backend.upper()}] bucket '{manager.config.bucket_name}' (prefix='{prefix}'):")
    items = manager.list_logs(prefix=prefix)
    if not items:
        print("  No logs found or endpoint unreachable.")
        return
    
    total_size = sum(item.get("Size", 0) for item in items)
    print(f"{'Remote Key':<60} | {'Size':<10} | {'Last Modified'}")
    print("-" * 95)
    for item in items:
        size_kb = item.get("Size", 0) / 1024.0
        size_str = f"{size_kb:.1f} KB" if size_kb >= 1 else f"{item.get('Size', 0)} B"
        print(f"{item.get('Key', ''):<60} | {size_str:<10} | {item.get('LastModified', '')}")
    print("-" * 95)
    print(f"Total: {len(items)} objects ({total_size / (1024*1024):.2f} MB)\n")


def upload_files(manager: LogStorageManager, file_paths: list[str], prefix: str = ""):
    if not manager.is_configured:
        print("[!] Storage is not configured. Set environment variables in .env first.")
        return

    ok_count = 0
    fail_count = 0
    for p in file_paths:
        path = Path(p)
        if not path.is_file():
            print(f"[-] Skipping non-file: {p}")
            continue
        remote_key = f"{prefix.rstrip('/')}/{path.name}" if prefix else path.name
        print(f"[+] Uploading {path} -> s3://{manager.config.bucket_name}/{remote_key} ...", end=" ", flush=True)
        if manager.upload_file(path, remote_key=remote_key):
            print("DONE")
            ok_count += 1
        else:
            print("FAILED")
            fail_count += 1
    print(f"\nUpload complete: {ok_count} succeeded, {fail_count} failed.\n")


def upload_archives(manager: LogStorageManager):
    archive_dirs = [d for d in ROOT_DIR.iterdir() if d.is_dir() and d.name.startswith("archive")]
    if not archive_dirs:
        print("No archive directories found.")
        return

    print(f"Found {len(archive_dirs)} archive directories. Uploading files...")
    ok_count = 0
    fail_count = 0
    for ad in sorted(archive_dirs):
        for f in ad.glob("**/*"):
            if f.is_file() and not f.name.endswith("-wal") and not f.name.endswith("-shm"):
                rel_path = f.relative_to(ROOT_DIR)
                remote_key = str(rel_path)
                print(f"[+] Uploading {rel_path} -> s3://{manager.config.bucket_name}/{remote_key} ...", end=" ", flush=True)
                if manager.upload_file(f, remote_key=remote_key):
                    print("DONE")
                    ok_count += 1
                else:
                    print("FAILED")
                    fail_count += 1
    print(f"\nArchive upload complete: {ok_count} succeeded, {fail_count} failed.\n")


def download_logs(manager: LogStorageManager, prefix: str, out_dir: str):
    items = manager.list_logs(prefix=prefix)
    if not items:
        print(f"No objects found with prefix '{prefix}'.")
        return

    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(items)} objects with prefix '{prefix}' to {dest_dir}...")
    for item in items:
        k = item["Key"]
        target = dest_dir / k
        print(f"[+] Downloading {k} -> {target} ...", end=" ", flush=True)
        if manager.download_file(k, target):
            print("DONE")
        else:
            print("FAILED")
    print("\nDownload complete.\n")


def main():
    parser = argparse.ArgumentParser(description="Trading Bot Log Storage Sync (Cloudflare R2 & MinIO)")
    parser.add_argument("--env", choices=["demo", "prod", "production"], default=None, help="Target environment (prod=R2, demo=MinIO)")
    parser.add_argument("--backend", choices=["r2", "minio", "local"], default=None, help="Explicit backend override")
    parser.add_argument("--status", action="store_true", help="Check connection and bucket health")
    parser.add_argument("--list", action="store_true", help="List remote logs in bucket")
    parser.add_argument("--prefix", type=str, default="", help="Prefix filter for list/upload/download")
    parser.add_argument("--upload", nargs="*", default=None, help="Upload specific local file(s)")
    parser.add_argument("--upload-recent", action="store_true", help="Upload root parallel_run.log, DBs, and meta")
    parser.add_argument("--upload-archives", action="store_true", help="Upload all historical archive directories")
    parser.add_argument("--download-prefix", type=str, default=None, help="Download all remote files matching prefix")
    parser.add_argument("--output-dir", type=str, default="./downloaded_logs", help="Destination folder for downloads")
    args = parser.parse_args()

    config = get_default_config(env=args.env, backend_override=args.backend)
    manager = LogStorageManager(config=config)

    if args.status or (not any([args.list, args.upload is not None, args.upload_recent, args.upload_archives, args.download_prefix])):
        print_status(manager)
        if not any([args.list, args.upload is not None, args.upload_recent, args.upload_archives, args.download_prefix]):
            return

    if args.list:
        list_remote_logs(manager, prefix=args.prefix)

    if args.upload is not None and len(args.upload) > 0:
        upload_files(manager, args.upload, prefix=args.prefix)

    if args.upload_recent:
        uploaded = manager.upload_run_logs(run_tag=args.prefix or "recent_run")
        print(f"Uploaded {len(uploaded)} run log/db files to object storage.")

    if args.upload_archives:
        upload_archives(manager)

    if args.download_prefix:
        download_logs(manager, prefix=args.download_prefix, out_dir=args.output_dir)


if __name__ == "__main__":
    main()
