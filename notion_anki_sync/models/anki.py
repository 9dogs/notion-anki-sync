"""Anki data models."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel


class ResponseSchema(BaseModel):
    """Anki response schema."""

    #: Result
    result: Optional[Union[int, list, Dict[str, Any]]]
    #: Error
    error: Optional[str]


@dataclass
class Image:
    """An image from HTML document."""

    #: `src` attribute as is in HTML document
    src: str
    #: Absolute path to the image
    abs_path: Path


@dataclass
class Note:
    """Anki note model."""

    #: Id
    id: str
    #: Front side
    front: str
    #: Back side
    back: str
    #: Tags
    tags: Optional[List[str]] = None
    #: Link to Notion page
    source: Optional[str] = None
    #: Note images
    images: Optional[List[Image]] = None
