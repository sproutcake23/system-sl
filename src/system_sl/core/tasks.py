import html
import logging
import random
import uuid
from datetime import datetime

from system_sl.utils import get_tasks_file_path
from system_sl.utils import load_data, save_data
from system_sl.utils.atomic_json import LoadStatus
from system_sl.utils.file_lock import file_lock
from system_sl.utils.json_client import load_data_result


log = logging.getLogger(__name__)


TASKS_FILE_PATH = get_tasks_file_path("tasks.json")
TASK_ORDER_FILE_PATH = get_tasks_file_path("task_order.json")
COMPLETED_TASKS_FILE_PATH = get_tasks_file_path("completed_tasks.json")
# Shared advisory lock. The GUI and the --bg notifier both mutate these files,
# so every read-modify-write sequence must hold this across both processes.
TASKS_LOCK_PATH = get_tasks_file_path("tasks.lock")


def sanitize_title(title: str) -> str:
    """Return a task title as the Player wrote it.

    Titles are stored literally: escaping at rest turned "AT&T" into
    "AT&amp;T", which then could not be matched, edited or completed. Any
    window that renders a title as rich text escapes it at that point.
    """
    if not isinstance(title, str):
        return ""
    return title.strip()


def _new_task_id() -> str:
    return uuid.uuid4().hex


def _migrate_tasks(tasks: list) -> bool:
    """Give every task an id and undo old HTML escaping. Returns True if changed."""
    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if not task.get("id"):
            task["id"] = _new_task_id()
            changed = True
        title = task.get("title")
        if isinstance(title, str):
            unescaped = html.unescape(title)
            if unescaped != title:
                task["title"] = unescaped
                changed = True
        # Google-sourced tasks carry the remote id; keep the name stable.
        if task.get("source_id") and not isinstance(task.get("source_id"), str):
            task["source_id"] = str(task["source_id"])
            changed = True
    return changed


def load_tasks():
    """Fetches active tasks from storage and automatically processes system migrations.

    Does NOT auto-save on corrupt read — corrupt files are backed up by
    safe_load_json and an empty list is returned. The caller decides whether
    to persist.
    """
    # Use load_data_result to distinguish corrupt/unreadable from empty
    result = load_data_result(TASKS_FILE_PATH)
    if result.status is LoadStatus.UNREADABLE:
        # Already logged and queued for the GUI by load_data_result().
        return []
    raw = result.data

    if raw is None:
        # File missing, empty, or corrupt (backed up to .corrupt-<timestamp>)
        # Do NOT auto-save [] — that would destroy recoverable data
        return []

    if not isinstance(raw, dict):
        # Already a list (current format) or unexpected type
        if isinstance(raw, list):
            if _migrate_tasks(raw):
                save_tasks(raw)
            return raw
        return []

    # Legacy dict format: {"category": [tasks...]} — migrate to flat list
    new_data = []
    migrated = False
    for category, task_list in raw.items():
        if not isinstance(task_list, list):
            continue
        for task in task_list:
            if isinstance(task, str):
                new_data.append(
                    {
                        "id": _new_task_id(),
                        "title": task,
                        "created_at": datetime.now().isoformat(),
                        "deadline": None,
                        "category": category,
                    }
                )
                migrated = True
            elif isinstance(task, dict):
                new_data.append(task)

    if migrated or new_data:
        # A dict is always the legacy format; persist the flat-list conversion.
        # (safe_load_json returns None for corruption, so reaching here with a
        # dict means the file really was a legacy dict, not a broken read.)
        save_tasks(new_data)

    _migrate_tasks(new_data)
    if new_data:
        save_tasks(new_data)
    return new_data


def save_tasks(tasks: dict|list):
    """Persists active tasks directly to the main tracking database file.

    Args:
        tasks (dict): Categorized active dictionary data structure to write.

    Returns:
        None
    """
    save_data(TASKS_FILE_PATH, tasks)


def _find_task(tasks: list, key: str):
    """Find a task by stable id, falling back to its title for old callers."""
    if not key:
        return None
    for task in tasks:
        if isinstance(task, dict) and task.get("id") == key:
            return task
    for task in tasks:
        if isinstance(task, dict) and task.get("title") == key:
            return task
    return None


def add_tasks(
    task_type: str = "none",
    task_title: str = "none",
    deadline: str = None,
    source_id: str | None = None,
):
    """Register a task, or update the deadline of an existing one.

    Args:
        task_type: Category bucket.
        task_title: Task text.
        deadline: Optional deadline.
        source_id: Stable id from an external source (Google Tasks). A task
            with the same source id is updated instead of duplicated.

    Returns:
        str: The title that was saved.
    """
    if not isinstance(task_title, str) or not task_title.strip():
        raise ValueError("Task title must be a non-empty string")

    task_title = sanitize_title(task_title)

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()

        if source_id:
            existing = next(
                (t for t in tasks if t.get("source_id") == source_id), None
            )
            if existing is not None:
                if deadline and existing.get("deadline") != deadline:
                    existing["deadline"] = deadline
                    save_tasks(tasks)
                    log.info(f"Updated deadline for '{task_title}' to {deadline}")
                return task_title

        for task in tasks:
            if task.get("title") == task_title:
                if deadline and task.get("deadline") != deadline:
                    task["deadline"] = deadline
                    save_tasks(tasks)

                    log.info(f"Updated deadline for '{task_title}' to {deadline}")
                    return task_title

                raise ValueError(f"Task '{task_title}' already exists")

        if isinstance(task_type, str):
            task_type = task_type.lower().strip()
        else:
            task_type = "None"

        new_task = {
            "id": _new_task_id(),
            "title": task_title,
            "created_at": datetime.now().isoformat(),
            "deadline": deadline,
            "category": task_type,
        }
        if source_id:
            new_task["source_id"] = str(source_id)

        tasks.append(new_task)
        save_tasks(tasks)
    return task_title


def remove_tasks(
    task_title: str | None = None,
    task_type: str = "none",
    *,
    task_id: str | None = None,
):
    """Remove a task by stable id (preferred) or by title.

    Returns:
        str: The removed task's title.
    """
    key = task_id or task_title
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Task id or title must be a non-empty string")

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()
        task = _find_task(tasks, key)
        if task is None:
            raise ValueError(f"Task '{key}' not found")

        removed_title = task.get("title", "")
        removed_id = task.get("id")
        tasks = [t for t in tasks if t is not task]
        save_tasks(tasks)
        _remove_from_task_order(removed_id, removed_title)
    return removed_title


def _remove_from_task_order(task_id: str | None, task_title: str) -> None:
    """Drop a task from the persisted manual ordering file.

    The order file is being migrated from titles to ids, so entries matching
    either are removed.
    """
    order_data = load_data(TASK_ORDER_FILE_PATH)
    if not isinstance(order_data, dict) or "order" not in order_data:
        return
    current_order = order_data["order"]
    filtered = [
        entry
        for entry in current_order
        if entry != task_id and entry != task_title
    ]
    if len(filtered) != len(current_order):
        save_data(TASK_ORDER_FILE_PATH, {"order": filtered})


def get_random_task():
    """Picks an outstanding item completely at random across all active tasks.

    Returns:
        str or None: A task title if items exist, otherwise None.
    """
    tasks = load_tasks()
    if not tasks:
        return None

    rand_task_obj = random.choice(tasks)

    return rand_task_obj["title"]


def save_manual_order(tasks: list) -> None:
    """Persist the manual task order atomically.

    Ids are preferred; titles remain accepted while the order file migrates.
    """
    order = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        key = task.get("id") or task.get("title")
        if key:
            order.append(key)
    with file_lock(TASKS_LOCK_PATH):
        save_data(TASK_ORDER_FILE_PATH, {"order": order})


def get_topn_task():
    """Returns up to 3 task titles from the persisted manual order.

    Cross-validates against current active tasks to prevent ghost notifications
    for tasks that have been removed or completed.

    Returns:
        list[str]: Up to 3 task titles, filtered to only include active tasks.
    """
    with file_lock(TASKS_LOCK_PATH):
        active_tasks = load_tasks()
        by_id = {
            t["id"]: t["title"]
            for t in active_tasks
            if isinstance(t, dict) and t.get("id")
        }
        by_title = {
            t["title"]
            for t in active_tasks
            if isinstance(t, dict) and t.get("title")
        }

        order_data = load_data(TASK_ORDER_FILE_PATH)
        if (
            not isinstance(order_data, dict)
            or "order" not in order_data
            or len(order_data["order"]) == 0
        ):
            # Pure fallback: use the stored task order in memory. Writing here
            # meant a read-only call (the notifier asks for the top tasks)
            # created and rewrote the user's ordering as a side effect.
            entries = [t["title"] for t in active_tasks if isinstance(t, dict)]
        else:
            entries = order_data["order"]

    resolved = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        title = by_id.get(entry) or (entry if entry in by_title else None)
        if title and title not in resolved:
            resolved.append(title)

    return resolved[:3]


def load_completed_tasks():
    """Fetches completed tasks from storage and processes legacy string-format migrations.

    Returns:
        list: A list of completed task dicts containing title, completed_at, and category fields.
    """
    with file_lock(TASKS_LOCK_PATH):
        data = load_data(COMPLETED_TASKS_FILE_PATH)
        new_data = []
        if isinstance(data, dict):
            migrated = False
            for category, task_list in data.items():
                if not isinstance(task_list, list):
                    continue
                for task in task_list:
                    if isinstance(task, str):
                        new_data.append(
                            {
                                "title": task,
                                "created_at": datetime.now().isoformat(),
                                "category": category,
                            }
                        )
                        migrated = True
                    elif isinstance(task, dict):
                        new_data.append(task)
            data = new_data

            if migrated:
                save_completed_tasks(new_data)
    return data


def save_completed_tasks(tasks: dict):
    """Persists historical completion statistics changes directly onto file records.

    Args:
        tasks (dict): Updated logs structure tracking archived items.

    Returns:
        None
    """
    save_data(COMPLETED_TASKS_FILE_PATH, tasks)


def edit_task(
    old_title: str | None,
    new_title: str,
    new_deadline: str = None,
    new_category: str | None = None,
    *,
    task_id: str | None = None,
) -> str:
    """Edit a task by stable id (preferred) or by title.

    ``new_category=None`` means "leave the category unchanged"; passing a
    category replaces it. This distinction matters because the edit dialog
    used to send ``None`` for an untouched field and wiped the category.

    Returns:
        str: The new task title.
    """
    key = task_id or old_title
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Task id or title must be a non-empty string")
    if not isinstance(new_title, str) or not new_title.strip():
        raise ValueError("New task title must be a non-empty string")

    new_title = sanitize_title(new_title)

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()
        task = _find_task(tasks, key)
        if task is None:
            raise ValueError(f"Task '{key}' not found")

        old_title = task.get("title", "")
        if new_title != old_title:
            for other in tasks:
                if other is not task and other.get("title") == new_title:
                    raise ValueError(f"Task '{new_title}' already exists")

        task["title"] = new_title
        if new_deadline is not None:
            task["deadline"] = new_deadline
        if new_category is not None:
            task["category"] = (
                new_category.lower().strip()
                if isinstance(new_category, str)
                else "none"
            )

        save_tasks(tasks)
        # The order file may still hold the old title; ids are stable.
        order_data = load_data(TASK_ORDER_FILE_PATH)
        if isinstance(order_data, dict) and "order" in order_data:
            order = [
                new_title if entry == old_title else entry
                for entry in order_data["order"]
            ]
            save_data(TASK_ORDER_FILE_PATH, {"order": order})

    return new_title


def mark_task_completed(
    task_title: str | None = None,
    task_type: str = "none",
    *,
    task_id: str | None = None,
):
    """Move a task to completed history, keeping its real category.

    Returns:
        str: The title of the task moved to history.
    """
    key = task_id or task_title
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Task id or title must be a non-empty string")

    with file_lock(TASKS_LOCK_PATH):
        task = _find_task(load_tasks(), key)
        if task is None:
            raise ValueError(f"Task '{key}' not found")
        real_title = task.get("title", "")
        real_category = task.get("category") or task_type
        real_id = task.get("id")

    removed_title = remove_tasks(task_id=real_id) if real_id else remove_tasks(real_title)

    with file_lock(TASKS_LOCK_PATH):
        completed_tasks = load_completed_tasks()

        completion_entry = {
            "title": removed_title,
            "completed_at": datetime.now().strftime("%Y-%m-%d"),
            "category": real_category,
        }
        if real_id:
            completion_entry["id"] = real_id
        if completion_entry not in completed_tasks:
            completed_tasks.append(completion_entry)

        save_completed_tasks(completed_tasks)
    return removed_title


if __name__ == "__main__":
    log.info("%s", get_topn_task())
