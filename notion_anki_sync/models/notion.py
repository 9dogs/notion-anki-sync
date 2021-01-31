"""Notion data models."""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class EnqueueTaskSchema(BaseModel):
    """Enqueue task response schema."""

    #: Task id
    task_id: str = Field(..., alias='taskId')


class TaskStatus(BaseModel):
    """Task status schema."""

    class Type(Enum):
        """Status types."""

        #: In progress
        IN_PROGRESS = 'progress'
        #: Complete
        COMPLETE = 'complete'

    #: Status type
    type: Type
    #: Num of pages exported so far
    pages_exported: int = Field(..., alias='pagesExported')
    #: File URL if completed
    export_url: Optional[str] = Field(default=None, alias='exportURL')


class TaskResult(BaseModel):
    """Task result schema."""

    class State(Enum):
        """Task state."""

        #: In progress
        IN_PROGRESS = 'in_progress'
        #: Successfully completed
        SUCCESS = 'success'

    #: Task id
    id: str
    #: Task state (in_progress | success)
    state: State
    #: Task status
    status: Optional[TaskStatus]


class TaskResults(BaseModel):
    """Get task response schema."""

    #: Results
    results: List[TaskResult]
