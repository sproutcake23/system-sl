"""Module for synchronizing external data sources using the Dependency Inversion Principle."""

import datetime
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from system_sl.core.tasks import add_tasks
from system_sl.utils import GoogleAPIClient

SCOPES: List[str] = [
    "https://www.googleapis.com/auth/tasks.readonly",
]


# =====================================================================
# 1. THE INTERFACE (Abstraction)
# =====================================================================
class GoogleProvider(ABC):
    """Abstract Base Class acting as an interface for external Google data sync providers."""

    def __init__(self, client: GoogleAPIClient, source_name: str) -> None:
        self.client: GoogleAPIClient = client
        self.source_name: str = source_name

    @abstractmethod
    def fetch_raw_data(self, time_max_iso: str, max_results: int) -> List[Dict[str, Any]]:
        """Fetches raw data payload from the specific Google API service."""
        pass

    @abstractmethod
    def extract_item_details(self, raw_item: Dict[str, Any]) -> tuple[str, Optional[str]]:
        """Extracts the uniform (title, raw_deadline) from a service-specific dictionary structure."""
        pass


# =====================================================================
# 2. CONCRETE IMPLEMENTATIONS (Polymorphism)
# =====================================================================
class TasksProvider(GoogleProvider):
    """Concrete implementation for syncing Google Tasks."""

    def __init__(self, client: GoogleAPIClient) -> None:
        super().__init__(client, source_name="task")

    def fetch_raw_data(self, time_max_iso: str, max_results: int) -> List[Dict[str, Any]]:
        return self.client.fetch_raw_tasks(time_max_iso=time_max_iso, max_results=max_results)

    def extract_item_details(self, raw_item: Dict[str, Any]) -> tuple[str, Optional[str]]:
        title = raw_item.get("title", "Untitled Task")
        deadline = raw_item.get("due")
        return title, deadline


# =====================================================================
# 3. THE UNIFIED SYNC ENGINE (High-Level Module)
# =====================================================================
class GoogleSyncEngine:
    """Orchestrates synchronization workflows generically using GoogleProvider abstractions."""

    def __init__(self, api_client: Optional[GoogleAPIClient] = None) -> None:
        self.client: GoogleAPIClient = api_client or GoogleAPIClient(scopes=SCOPES)

    def _format_deadline(self, deadline_str: Optional[str]) -> Optional[str]:
        """Converts incoming varied Google timestamp strings into a uniform local format."""
        if not deadline_str:
            return None
        if len(deadline_str) == 10:
            return f"{deadline_str} 00:00"
        try:
            clean_str = deadline_str.replace("Z", "+00:00")
            dt_obj = datetime.datetime.fromisoformat(clean_str)
            return dt_obj.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return deadline_str

    def _process_and_add_task(self, source_type: str, title: str, deadline: Optional[str]) -> int:
        """Saves individual tasks to core storage, ignoring duplicates."""
        try:
            add_tasks(source_type, title, deadline=deadline)
            print(f" [+] Imported [{source_type}]: {title} (Due: {deadline})")
            return 1
        except ValueError:
            print(f" [x] Skipped (Duplicate): {title}")
            return 0

    def execute_sync(self, provider: GoogleProvider, max_results: int = 10) -> tuple[int, int]:
        """Executes a sync lifecycle for any provider implementing the GoogleProvider interface.

        Args:
            provider (GoogleProvider): The concrete data provider subclass to sync.
            max_results (int): Entry cap constraint per execution.

        Returns:
            tuple[int, int]: (imported, skipped_as_duplicate) counts.

        Raises:
            GoogleSyncError: If authentication fails or the API cannot be reached.
        """
        print(f"Connecting to Google Service for [{provider.source_name}]...")
        now = datetime.datetime.utcnow()
        six_months_later = now + datetime.timedelta(days=182)
        time_max_iso = six_months_later.isoformat() + "Z"

        print(f"Fetching {provider.source_name} data until {six_months_later.strftime('%Y-%m-%d')}...")
        raw_items = provider.fetch_raw_data(time_max_iso, max_results)

        if not raw_items:
            print(f"No upcoming items found for {provider.source_name}.")
            return 0, 0

        count = 0
        for item in raw_items:
            title, raw_deadline = provider.extract_item_details(item)
            clean_deadline = self._format_deadline(raw_deadline)
            count += self._process_and_add_task(provider.source_name, title, clean_deadline)

        print(f"\n[{provider.source_name}] Sync complete. {count} items processed.")
        return count, len(raw_items) - count


# Example execution block
if __name__ == "__main__":
    # Create the sync manager
    engine = GoogleSyncEngine()
    
    engine.execute_sync(TasksProvider(engine.client))