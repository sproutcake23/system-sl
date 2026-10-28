"""Module for managing authentication and data retrieval from Google APIs."""

import json
import os.path
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from system_sl.utils import secure_storage




class GoogleAPIClient:
    """Handles authentication and fetches raw data from Google Calendar and Google Tasks APIs."""

    def __init__(self, scopes: List[str]) -> None:
        """Initializes the Google API client with required OAuth scopes.

        Args:
            scopes (List[str]): A list of Google OAuth scope URIs needed for permissions.
        """

        from system_sl.core.tasks import get_tasks_file_path
        
        self.scopes: List[str] = scopes
        self._creds_path: str = get_tasks_file_path("credentials.json")
        self._token_path: str = get_tasks_file_path("token.json")

    def _get_credentials(self) -> Optional[Credentials]:
        """Handles user authentication, reading cached tokens, or initiating local OAuth flows.

        The access token and refresh token are stored in the system keyring
        (KDE Wallet / GNOME Keyring / macOS Keychain / Windows Credential
        Manager) with an encrypted-file fallback, never as plaintext
        ``token.json`` on disk.

        Returns:
            Optional[Credentials]: Valid Google OAuth credentials if successful, None otherwise.
        """
        creds: Optional[Credentials] = None

        # Load cached token from secure storage.
        token_data = secure_storage.get_json_secret(secure_storage.SECRET_GOOGLE_TOKEN)
        if token_data:
            try:
                creds = Credentials.from_authorized_user_info(token_data, self.scopes)
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Obtain the client config from secure storage, falling back to
                # a legacy credentials.json file on disk.
                client_data = secure_storage.get_json_secret(
                    secure_storage.SECRET_GOOGLE_CREDENTIALS
                )
                if client_data is not None:
                    flow = InstalledAppFlow.from_client_config(client_data, self.scopes)
                elif os.path.exists(self._creds_path):
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self._creds_path, self.scopes
                    )
                else:
                    print(
                        "ERROR: Google OAuth credentials not found. "
                        "Provide credentials.json or set the google-oauth-credentials secret."
                    )
                    return None

                try:
                    # Open a local browser with a timeout so the app never
                    # hangs waiting for a redirect that never arrives.
                    creds = flow.run_local_server(port=0, timeout_seconds=120, open_browser=True)
                except Exception:
                    # Headless environment (no browser / no display): fall back
                    # to the out-of-band console flow.
                    print("No browser available — falling back to console OAuth flow.")
                    creds = flow.run_console()

            # Persist the acquired/refreshed token securely, never in plaintext.
            if creds:
                secure_storage.set_json_secret(
                    secure_storage.SECRET_GOOGLE_TOKEN,
                    json.loads(creds.to_json()),
                )

        return creds

    def fetch_raw_calendar_events(self, time_min_iso: str, time_max_iso: str, max_results: int) -> List[Dict[str, Any]]:
        """Queries the Google Calendar API for events within a specified timeframe.

        Args:
            time_min_iso (str): The lower bound (inclusive) for an event's start time in ISO-8601 format.
            time_max_iso (str): The upper bound (exclusive) for an event's start time in ISO-8601 format.
            max_results (int): Maximum number of events to return.

        Returns:
            List[Dict[str, Any]]: List of dictionary structures containing raw event properties.
        """
        creds: Optional[Credentials] = self._get_credentials()
        if not creds:
            return []

        service: Resource = build("calendar", "v3", credentials=creds)
        try:
            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min_iso,
                    timeMax=time_max_iso,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return events_result.get("items", [])
        except Exception as e:
            print(f"API Error fetching calendar events: {e}")
            return []

    def fetch_raw_tasks(self, time_max_iso: str, max_results: int) -> List[Dict[str, Any]]:
        """Queries the Google Tasks API for incomplete items across all user task lists.

        Args:
            time_max_iso (str): The upper bound (inclusive) for a task's due date in ISO-8601 format.
            max_results (int): Maximum number of items to return per task list.

        Returns:
            List[Dict[str, Any]]: Aggregated list of raw task item dictionaries.
        """
        creds: Optional[Credentials] = self._get_credentials()
        if not creds:
            return []

        service: Resource = build("tasks", "v1", credentials=creds)
        all_tasks: List[Dict[str, Any]] = []
        
        try:
            lists_result = service.tasklists().list(maxResults=10).execute()
            task_lists = lists_result.get("items", [])
        except Exception as e:
            print(f"API Error fetching task lists: {e}")
            return []

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