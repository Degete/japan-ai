"""Data models for the agent platform."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time


class Priority(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskRequest:
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
