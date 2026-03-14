import base64
import json
import os
from typing import Optional
from uuid import uuid4

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account


class GoogleDriveStore:
    def __init__(
        self,
        folder_id: str,
        service_account_payload: str = "",
        oauth_client_id: str = "",
        oauth_client_secret: str = "",
        oauth_refresh_token: str = "",
    ):
        if not folder_id:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID is required")
        if not service_account_payload and not (oauth_client_id and oauth_client_secret and oauth_refresh_token):
            raise ValueError("Google Drive credentials are required")
        self.folder_id = folder_id
        self._credentials = self._build_credentials(
            service_account_payload=service_account_payload,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            oauth_refresh_token=oauth_refresh_token,
        )

    @staticmethod
    def _parse_service_account_info(payload: str) -> dict:
        raw = payload.strip()
        if not raw:
            raise ValueError("Google service account payload is empty")
        if raw.startswith("{"):
            return json.loads(raw)
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)

    def _build_credentials(
        self,
        service_account_payload: str,
        oauth_client_id: str,
        oauth_client_secret: str,
        oauth_refresh_token: str,
    ):
        if oauth_client_id and oauth_client_secret and oauth_refresh_token:
            return Credentials(
                token=None,
                refresh_token=oauth_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=oauth_client_id,
                client_secret=oauth_client_secret,
                scopes=["https://www.googleapis.com/auth/drive"],
            )
        info = self._parse_service_account_info(service_account_payload)
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )

    def _authorized_headers(self) -> dict:
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return {"Authorization": f"Bearer {self._credentials.token}"}

    @classmethod
    def from_env(cls) -> "GoogleDriveStore":
        payload = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        payload_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        if (not payload) and payload_file:
            with open(payload_file, "r", encoding="utf-8") as f:
                payload = f.read()
        return cls(
            folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip(),
            service_account_payload=payload,
            oauth_client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            oauth_client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            oauth_refresh_token=os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip(),
        )

    def _find_file_id(self, remote_name: str) -> Optional[str]:
        query = (
            f"name = '{remote_name}' and "
            f"'{self.folder_id}' in parents and trashed = false"
        )
        response = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=self._authorized_headers(),
            params={
                "q": query,
                "fields": "files(id,name,modifiedTime)",
                "orderBy": "modifiedTime desc",
                "pageSize": 10,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
            timeout=30,
        )
        response.raise_for_status()
        files = response.json().get("files", [])
        return files[0]["id"] if files else None

    def download_if_exists(self, remote_name: str, local_path: str) -> bool:
        file_id = self._find_file_id(remote_name)
        if not file_id:
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        response = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers=self._authorized_headers(),
            params={"alt": "media", "supportsAllDrives": "true"},
            timeout=120,
        )
        response.raise_for_status()
        with open(local_path, "wb") as fh:
            fh.write(response.content)
        return True

    def upload_or_replace(self, local_path: str, remote_name: str, mime_type: Optional[str] = None) -> str:
        if not os.path.exists(local_path):
            raise FileNotFoundError(local_path)
        content_type = mime_type or "application/octet-stream"
        existing_id = self._find_file_id(remote_name)
        with open(local_path, "rb") as fh:
            data = fh.read()
        headers = self._authorized_headers()
        headers["Content-Type"] = content_type

        if existing_id:
            response = requests.patch(
                f"https://www.googleapis.com/upload/drive/v3/files/{existing_id}",
                headers=headers,
                params={"uploadType": "media", "supportsAllDrives": "true"},
                data=data,
                timeout=120,
            )
            if response.ok:
                return existing_id
            if response.status_code != 403:
                response.raise_for_status()

        boundary = f"==============={uuid4().hex}"
        metadata = json.dumps({"name": remote_name, "parents": [self.folder_id]})
        multipart_body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--".encode("utf-8")

        create_headers = self._authorized_headers()
        create_headers["Content-Type"] = f'multipart/related; boundary="{boundary}"'
        response = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            headers=create_headers,
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
            data=multipart_body,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["id"]
