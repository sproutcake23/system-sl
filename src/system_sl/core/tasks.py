import html
import logging
import random
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
    """Strip HTML/JS from a task title so it can never inject into rich text.

    Qt rich text renders any HTML it is given, so titles that ever reach
    ``setHtml()`` / markdown rendering must be escaped first.
    """
    if not isinstance(title, str):
        return ""
    return html.escape(title.strip())


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
        # File missing, empty, or corrupt (backed up to .corrupt)
        # Do NOT auto-save [] — that would destroy recoverable data
        return []

    if not isinstance(raw, dict):
        # Already a list (current format) or unexpected type
        return raw if isinstance(raw, list) else []

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

    return new_data


def save_tasks(tasks: dict|list):
    """Persists active tasks directly to the main tracking database file.

    Args:
        tasks (dict): Categorized active dictionary data structure to write.

    Returns:
        None
    """
    save_data(TASKS_FILE_PATH, tasks)


def add_tasks(task_type: str = "none",task_title: str = "none",  deadline: str = None):
    """Registers a new task inside a specific category pool while enforcing validation rules and avoiding duplicates.

    Args:
        task_type (str): The bucket name representing the task category.
        task_title (str): Summary description of the item to add.
        deadline (str, optional): Targeted deadline timestamp. Defaults to None.

    Returns:
        str: The sanitized task title string that was successfully saved.
    """
    if not isinstance(task_title, str) or not task_title.strip():
        raise ValueError("Task title must be a non-empty string")

    task_title = sanitize_title(task_title)

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()

        for task in tasks:
            if task["title"] == task_title:
                if deadline and task.get("deadline") != deadline:
                    task["deadline"] = deadline
                    save_tasks(tasks)

                    print(f"Updated deadline for '{task_title}' to {deadline}")
                    return task_title

                raise ValueError(f"Task '{task_title}' already exists")

        if isinstance(task_type, str):
            task_type = task_type.lower().strip()
        else:
            task_type = "None"

        new_task = {
            "title": task_title,
            "created_at": datetime.now().isoformat(),
            "deadline": deadline,
            "category": task_type,
        }

        tasks.append(new_task)
        save_tasks(tasks)
    return task_title


def remove_tasks(task_title: str, task_type: str = "none"):
    """Evicts a target task from active tracking files, popping the category if left empty.

    Args:
        task_type (str): The category container where the task resides.
        task_title (str): The exact text name of the task item to clean up.

    Returns:
        str: The title string of the successfully removed task.
    """
    if not isinstance(task_title, str) or not task_title.strip():
        raise ValueError("Task title must be a non-empty string")

    task_title = sanitize_title(task_title)

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()

        original_count = len(tasks)

        tasks = [t for t in tasks if t["title"] != task_title]

        if len(tasks) == original_count:
            raise ValueError(f"Task '{task_title}' not found")

        save_tasks(tasks)
        _remove_from_task_order(task_title)
    return task_title


def _remove_from_task_order(task_title: str) -> None:
    """Strips a task title from the persisted manual ordering file.

    Args:
        task_title (str): The exact title to remove from the order list.
    """
    order_data = load_data(TASK_ORDER_FILE_PATH)
    if not isinstance(order_data, dict) or "order" not in order_data:
        return
    current_order = order_data["order"]
    filtered = [t for t in current_order if t != task_title]
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

    Args:
        tasks: List of task dicts (each must have a "title" key).
    """
    order = [t.get("title", "") for t in tasks if isinstance(t, dict)]
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
        active_titles = {t["title"] for t in active_tasks if isinstance(t, dict)}

        order_data = load_data(TASK_ORDER_FILE_PATH)
        if (
            not isinstance(order_data, dict)
            or "order" not in order_data
            or len(order_data["order"]) == 0
        ):
            # Pure fallback: use the stored task order in memory. Writing here
            # meant a read-only call (the notifier asks for the top tasks)
            # created and rewrote the user's ordering as a side effect.
            ntasks = [t["title"] for t in active_tasks if isinstance(t, dict)]
        else:
            ntasks = order_data["order"]

    ntasks = [t for t in ntasks if t in active_titles and t.strip()]

    return ntasks[:3]


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


def edit_task(old_title: str, new_title: str, new_deadline: str = None, new_category: str = "none") -> str:
    """Edit an existing task's title, deadline, and/or category.

    Args:
        old_title: The current title of the task to edit.
        new_title: The new title for the task.
        new_deadline: Optional new deadline.
        new_category: Optional new category.

    Returns:
        str: The new (sanitized) task title.

    Raises:
        ValueError: If the old task is not found or the new title is empty.
    """
    if not isinstance(old_title, str) or not old_title.strip():
        raise ValueError("Old task title must be a non-empty string")
    if not isinstance(new_title, str) or not new_title.strip():
        raise ValueError("New task title must be a non-empty string")

    old_title = sanitize_title(old_title)
    new_title = sanitize_title(new_title)

    with file_lock(TASKS_LOCK_PATH):
        tasks = load_tasks()

        found = False
        for task in tasks:
            if task["title"] == old_title:
                found = True
                # If the title changed, check for duplicates against the new title
                if new_title != old_title:
                    for other in tasks:
                        if other is not task and other["title"] == new_title:
                            raise ValueError(f"Task '{new_title}' already exists")
                task["title"] = new_title
                if new_deadline is not None:
                    task["deadline"] = new_deadline
                task["category"] = new_category.lower().strip() if isinstance(new_category, str) else "none"
                break

        if not found:
            raise ValueError(f"Task '{old_title}' not found")

        save_tasks(tasks)
        # Rebuild manual order to reflect any title change
        order_data = load_data(TASK_ORDER_FILE_PATH)
        if isinstance(order_data, dict) and "order" in order_data:
            order = [
                new_title if t == old_title else t for t in order_data["order"]
            ]
            save_data(TASK_ORDER_FILE_PATH, {"order": order})

    return new_title


def mark_task_completed(task_title: str, task_type: str = "none"):
    """Extracts a task out of active runtime arrays and logs it as completed inside history archives.

    Args:
        task_type: Original classification bucket tracking the item.
        task_title: Unique text identity of the task being checked off.

    Returns:
        str: The title of the validated task moved to historical logs.
    """
    removed_title = remove_tasks(task_title, task_type)

    with file_lock(TASKS_LOCK_PATH):
        completed_tasks = load_completed_tasks()

        completion_entry = {
            "title": task_title,
            "completed_at": datetime.now().strftime("%Y-%m-%d"),
            "category": task_type,
        }
        if completion_entry not in completed_tasks:
            completed_tasks.append(completion_entry)

        save_completed_tasks(completed_tasks)
    return removed_title


if __name__ == "__main__":
    print(get_topn_task())
