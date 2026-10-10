"""Storage, validation, filtering, and reporting for the task dashboard.

This module only uses Python's standard library.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
import json
import os
import tempfile
import uuid

VALID_STATUSES = {"todo", "in_progress", "blocked", "done"}
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
MAX_TITLE_LENGTH = 150
MAX_DESCRIPTION_LENGTH = 2000


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: Any, *, max_length: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer")
    return result


def validate_due_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("due_date must be a YYYY-MM-DD string or null")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("due_date must use YYYY-MM-DD format") from exc


def validate_task_input(data: Any, *, partial: bool = False) -> dict[str, Any]:
    """Validate incoming task fields, disallowing unknown fields."""
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    allowed = {"title", "description", "status", "priority", "assignee", "tags", "due_date"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}")
    if not partial and "title" not in data:
        raise ValueError("title is required")

    result: dict[str, Any] = {}
    if "title" in data:
        title = normalize_text(data["title"], max_length=MAX_TITLE_LENGTH, field="title")
        if not title:
            raise ValueError("title cannot be empty")
        result["title"] = title
    if "description" in data:
        result["description"] = normalize_text(data["description"], max_length=MAX_DESCRIPTION_LENGTH, field="description")
    if "assignee" in data:
        result["assignee"] = normalize_text(data["assignee"], max_length=100, field="assignee")
    for key, choices in (("status", VALID_STATUSES), ("priority", VALID_PRIORITIES)):
        if key in data:
            if data[key] not in choices:
                raise ValueError(f"{key} must be one of {', '.join(sorted(choices))}")
            result[key] = data[key]
    if "due_date" in data:
        result["due_date"] = validate_due_date(data["due_date"])
    if "tags" in data:
        tags = data["tags"]
        if not isinstance(tags, list) or len(tags) > 15 or any(not isinstance(t, str) for t in tags):
            raise ValueError("tags must be a list of at most 15 strings")
        cleaned = [t.strip().lower() for t in tags]
        if any(not t or len(t) > 40 for t in cleaned):
            raise ValueError("each tag must be between 1 and 40 characters")
        result["tags"] = list(dict.fromkeys(cleaned))
    return result


class TaskNotFound(Exception):
    """Raised when an identifier does not exist."""


class TaskRepository:
    """Thread-safe JSON-file-backed task repository.

    Writes are atomic at the filesystem level: the file is replaced only after
    a complete, valid JSON document has been written to a temporary file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"project": "Task Dashboard", "tasks": []})
        self._read()  # Validate the existing file early.

    def _read(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as file:
            document = json.load(file)
        if not isinstance(document, dict) or not isinstance(document.get("tasks"), list):
            raise ValueError("Data file must be a JSON object containing a tasks array")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        encoded = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False
            ) as file:
                temp_name = file.name
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        query: str = "",
        sort_by: str = "created_at",
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        if status and status not in VALID_STATUSES:
            raise ValueError("Invalid status filter")
        if priority and priority not in VALID_PRIORITIES:
            raise ValueError("Invalid priority filter")
        if sort_by not in {"title", "created_at", "updated_at", "due_date", "priority", "status"}:
            raise ValueError("Invalid sort field")
        with self.lock:
            tasks = [dict(item) for item in self._read()["tasks"]]
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        if priority:
            tasks = [task for task in tasks if task.get("priority") == priority]
        if query.strip():
            term = query.casefold().strip()
            tasks = [task for task in tasks if any(
                term in str(task.get(key, "")).casefold()
                for key in ("title", "description", "assignee", "tags")
            )]
        order = {"low": 0, "medium": 1, "high": 2, "urgent": 3}
        if sort_by == "priority":
            tasks.sort(key=lambda item: order.get(item.get("priority"), -1), reverse=descending)
        else:
            tasks.sort(key=lambda item: str(item.get(sort_by) or "9999-12-31"), reverse=descending)
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            for task in self._read()["tasks"]:
                if task.get("id") == task_id:
                    return dict(task)
        raise TaskNotFound(task_id)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = validate_task_input(payload)
        timestamp = utc_now()
        task = {
            "id": uuid.uuid4().hex[:12],
            "title": fields.get("title"),
            "description": fields.get("description", ""),
            "status": fields.get("status", "todo"),
            "priority": fields.get("priority", "medium"),
            "assignee": fields.get("assignee", ""),
            "tags": fields.get("tags", []),
            "due_date": fields.get("due_date"),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        with self.lock:
            document = self._read()
            document["tasks"].append(task)
            self._write(document)
        return dict(task)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        changes = validate_task_input(payload, partial=True)
        with self.lock:
            document = self._read()
            for task in document["tasks"]:
                if task.get("id") == task_id:
                    task.update(changes)
                    task["updated_at"] = utc_now()
                    self._write(document)
                    return dict(task)
        raise TaskNotFound(task_id)

    def delete_task(self, task_id: str) -> None:
        with self.lock:
            document = self._read()
            remaining = [task for task in document["tasks"] if task.get("id") != task_id]
            if len(remaining) == len(document["tasks"]):
                raise TaskNotFound(task_id)
            document["tasks"] = remaining
            self._write(document)

    def summary(self) -> dict[str, Any]:
        tasks = self.list_tasks()
        statuses = Counter(task.get("status", "todo") for task in tasks)
        priorities = Counter(task.get("priority", "medium") for task in tasks)
        today = datetime.now(timezone.utc).date().isoformat()
        overdue = sum(1 for task in tasks if task.get("due_date") and task["due_date"] < today and task.get("status") != "done")
        done = statuses["done"]
        return {
            "total": len(tasks),
            "completed": done,
            "completion_percent": round(100 * done / len(tasks), 1) if tasks else 0,
            "overdue": overdue,
            "by_status": {key: statuses[key] for key in sorted(VALID_STATUSES)},
            "by_priority": {key: priorities[key] for key in sorted(VALID_PRIORITIES)},
        }
