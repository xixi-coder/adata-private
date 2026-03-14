import json
import os
import tarfile
import zlib

from jobs.common.archive_bundle import create_tar_gz, extract_tar_gz
from jobs.common.google_drive_store import GoogleDriveStore
from jobs.common.local_env import load_local_env

SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"


def drive_enabled() -> bool:
    load_local_env()
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    json_payload = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    json_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    oauth_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    oauth_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    oauth_refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    oauth_ready = bool(oauth_client_id and oauth_client_secret and oauth_refresh_token)
    return bool(folder_id and (json_payload or json_file or oauth_ready))


def sync_cache_from_drive(project_root: str, archive_name: str, cache_paths: list[str]) -> bool:
    if not drive_enabled():
        print("Google Drive not configured, skip cache download.")
        return False
    store = GoogleDriveStore.from_env()
    tmp_dir = os.path.join(project_root, "data", "cloud_sync")
    os.makedirs(tmp_dir, exist_ok=True)
    archive_path = os.path.join(tmp_dir, archive_name)
    found = store.download_if_exists(archive_name, archive_path)
    if not found:
        print(f"Google Drive cache bundle not found: {archive_name}")
        return False
    try:
        extract_tar_gz(archive_path, project_root)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        print(f"Google Drive cache bundle invalid, ignore and rebuild: {archive_name} ({exc})")
        return False
    for path in cache_paths:
        print(f"Restored path from Google Drive: {path}")
    return True


def sync_cache_to_drive(project_root: str, archive_name: str, cache_paths: list[str]) -> bool:
    if not drive_enabled():
        print("Google Drive not configured, skip cache upload.")
        return False
    store = GoogleDriveStore.from_env()
    tmp_dir = os.path.join(project_root, "data", "cloud_sync")
    os.makedirs(tmp_dir, exist_ok=True)
    archive_path = os.path.join(tmp_dir, archive_name)
    create_tar_gz(archive_path, cache_paths, project_root)
    store.upload_or_replace(archive_path, archive_name, mime_type="application/gzip")
    print(f"Uploaded cache bundle to Google Drive: {archive_name}")
    return True


def download_json_from_drive(project_root: str, remote_name: str, local_path: str) -> bool:
    if not drive_enabled():
        return False
    store = GoogleDriveStore.from_env()
    return store.download_if_exists(remote_name, local_path)


def upload_file_to_drive(local_path: str, remote_name: str, mime_type: str = "application/octet-stream") -> bool:
    if not drive_enabled():
        print(f"Google Drive not configured, skip upload: {remote_name}")
        return False
    store = GoogleDriveStore.from_env()
    store.upload_or_replace(local_path, remote_name, mime_type=mime_type)
    print(f"Uploaded file to Google Drive: {remote_name}")
    return True


def write_json(path: str, payload: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
