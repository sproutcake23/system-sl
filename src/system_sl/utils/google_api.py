"""Module for managing authentication and data retrieval from Google APIs."""

import json
import os.path
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from system_sl.utils import secure_storage

# Release builds ship the app's own OAuth client under this name in the
# bundled assets. CI writes it from the GOOGLE_OAUTH_CLIENT repository secret;
# it is gitignored and never committed.
BUNDLED_CLIENT_FILE = "google_oauth_client.json"


def _bundled_client_path() -> Path:
    """Returns where the bundled OAuth client lives for this kind of install."""
    if getattr(sys, "frozen", False):
        # PyInstaller copies src/system_sl/assets to <bundle>/assets.
        return Path(sys._MEIPASS) / "assets" / BUNDLED_CLIENT_FILE
    return Path(__file__).parent.parent / "assets" / BUNDLED_CLIENT_FILE


class GoogleSyncError(Exception):
    """Raised when Google sync cannot authenticate or reach the API.

    The message is user-facing and is shown as-is in the GUI.
    """


class GoogleAPIClient:
    """Handles authentication and fetches raw data from the Google Tasks API."""

    def __init__(self, scopes: List[str]) -> None:
        """Initializes the Google API client with required OAuth scopes.

        Args:
            scopes (List[str]): A list of Google OAuth scope URIs needed for permissions.
        """

        from system_sl.core.tasks import get_tasks_file_path

        self.scopes: List[str] = scopes
        self._creds_path: str = get_tasks_file_path("credentials.json")

    def _get_credentials(self) -> Credentials:
        """Handles user authentication, reading cached tokens, or initiating local OAuth flows.

        The access token and refresh token are stored in the system keyring
        (KDE Wallet / GNOME Keyring / macOS Keychain / Windows Credential
        Manager) with an encrypted-file fallback, never as plaintext
        ``token.json`` on disk.

        Returns:
            Credentials: Valid Google OAuth credentials.

        Raises:
            GoogleSyncError: If no OAuth client is configured or sign-in fails.
        """
        creds: Optional[Credentials] = None

        # Load cached token from secure storage.
        token_data = secure_storage.get_json_secret(secure_storage.SECRET_GOOGLE_TOKEN)
        if token_data:
            try:
                creds = Credentials.from_authorized_user_info(token_data, self.scopes)
            except Exception:
                creds = None

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # The refresh token was revoked or expired. Drop it so the
                # user gets a fresh sign-in instead of the same error forever.
                print("Stored Google token is no longer valid — signing in again.")
                secure_storage.delete_secret(secure_storage.SECRET_GOOGLE_TOKEN)
                creds = None
            else:
                self._store_token(creds)
                return creds

        creds = self._run_sign_in_flow()
        self._store_token(creds)
        return creds

    def _run_sign_in_flow(self) -> Credentials:
        """Runs the browser OAuth consent flow and returns the new credentials."""
        # A credentials.json the user set up themselves wins over the app's
        # own client shipped with release builds.
        bundled_path = _bundled_client_path()
        # PKCE is requested explicitly rather than left to the library
        # default: the bundled client secret ships inside every release, so
        # PKCE is what stops it being used to take over someone's sign-in.
        if os.path.exists(self._creds_path):
            flow = InstalledAppFlow.from_client_secrets_file(
                self._creds_path, self.scopes, autogenerate_code_verifier=True
            )
        elif bundled_path.exists():
            flow = InstalledAppFlow.from_client_secrets_file(
                str(bundled_path), self.scopes, autogenerate_code_verifier=True
            )
        else:
            raise GoogleSyncError(
                "Google OAuth credentials not found.\n\n"
                f"Place your credentials.json at:\n{self._creds_path}\n\n"
                "See the Google Tasks Sync page in the wiki for setup steps."
            )

        try:
            # Opens the browser and waits for the localhost redirect. The
            # sign-in URL is also printed to stdout in case no browser opens.
            return flow.run_local_server(port=0, timeout_seconds=120, open_browser=True)
        except Exception as e:
            raise GoogleSyncError(
                "Google sign-in did not complete.\n\n"
                "Finish the sign-in in your browser within 2 minutes, then try again.\n"
                f"({type(e).__name__}: {e})"
            ) from e

    def _store_token(self, creds: Credentials) -> None:
        """Persists the acquired/refreshed token securely, never in plaintext."""
        secure_storage.set_json_secret(
            secure_storage.SECRET_GOOGLE_TOKEN,
            json.loads(creds.to_json()),
        )

    def fetch_raw_tasks(self, time_max_iso: str, max_results: int) -> List[Dict[str, Any]]:
        """Queries the Google Tasks API for incomplete items across all user task lists.

        Args:
            time_max_iso (str): The upper bound (inclusive) for a task's due date in ISO-8601 format.
            max_results (int): Maximum number of items to return per task list.

        Returns:
            List[Dict[str, Any]]: Aggregated list of raw task item dictionaries.

        Raises:
            GoogleSyncError: If authentication fails or the task lists cannot be read.
        """
        creds: Credentials = self._get_credentials()

        service: Resource = build("tasks", "v1", credentials=creds)
        all_tasks: List[Dict[str, Any]] = []

        try:
            lists_result = service.tasklists().list(maxResults=10).execute()
            task_lists = lists_result.get("items", [])
        except Exception as e:
            raise GoogleSyncError(f"Could not read your Google task lists:\n{e}") from e

        for task_list in task_lists:
            try:
                tasks_result = (
                    service.tasks()
                    .list(
                        tasklist=task_list["id"],
                        maxResults=max_results,
                        dueMax=time_max_iso,
                        showHidden=False,
                    )
                    .execute()
                )
                all_tasks.extend(tasks_result.get("items", []))
            except Exception as e:
                print(f"API Error fetching tasks from list '{task_list.get('title')}': {e}")
                continue

        return all_tasks
