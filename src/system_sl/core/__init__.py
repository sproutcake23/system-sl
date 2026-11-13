"""Task storage, ranking and Google sync.

Names are loaded on first use: importing ``system_sl.core.tasks`` runs this
file, and loading the sync service here pulled the Google API client into
every process that only reads tasks.
"""

from importlib import import_module

_EXPORTS = {
    "GoogleSyncEngine": "system_sl.core.sync_service",
    "TasksProvider": "system_sl.core.sync_service",
    "load_tasks": "system_sl.core.tasks",
    "add_tasks": "system_sl.core.tasks",
    "remove_tasks": "system_sl.core.tasks",
    "get_random_task": "system_sl.core.tasks",
    "load_completed_tasks": "system_sl.core.tasks",
    "save_completed_tasks": "system_sl.core.tasks",
    "mark_task_completed": "system_sl.core.tasks",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value  # later lookups skip __getattr__
    return value
