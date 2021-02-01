"""Notion and Anki clients."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, List, Optional, Type

import aiofiles
import aiohttp
import structlog
from aiohttp import ClientConnectorError, ClientOSError

from notion_anki_sync.config import Config
from notion_anki_sync.models.anki import Note, ResponseSchema
from notion_anki_sync.models.notion import (
    EnqueueTaskSchema,
    TaskResult,
    TaskResults,
)


class BaseClient:
    """Basic aiohttp-based client."""

    def __init__(self, config: Config):
        """Init the client.

        :param config: configuration
        """
        self.logger = structlog.getLogger(f'sync.{self.__class__.__name__}')
        self.config = config
        self.cookies: Optional[Dict[str, str]] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> BaseClient:
        """Create a session and return the client."""
        timeout = aiohttp.ClientTimeout(total=5)
        self.session = aiohttp.ClientSession(
            cookies=self.cookies, timeout=timeout
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Optional[bool]:
        """Close the session.

        :param exc_type: exception type
        :param exc: exception
        :param tb: traceback
        :returns: exception or None
        """
        assert self.session  # mypy
        await self.session.close()
        return None


class NotionClient(BaseClient):
    """Notion client."""

    def __init__(self, config: Config):
        """Init the client.

        :param config: configuration
        """
        super().__init__(config)
        self.cookies = {'token_v2': self.config.NOTION_TOKEN}

    async def enqueue_export_task(
        self, page_id: str, recursive: bool = False
    ) -> str:
        """Enqueue an export task for a given page.

        :param page_id: page id
        :param recursive: use recursive export
        :returns: task id
        """
        payload = {
            'task': {
                'eventName': 'exportBlock',
                'request': {
                    'blockId': page_id,
                    'recursive': recursive,
                    'exportOptions': {
                        'exportType': 'html',
                        'timeZone': 'Europe/Moscow',
                        'locale': 'en',
                    },
                },
            }
        }
        assert self.session  # mypy
        async with self.session.post(
            self.config.NOTION_ENQUEUE_TASK_ENDPOINT, json=payload
        ) as response:
            if response.status == 401:
                self.logger.error('Invalid token')
            response.raise_for_status()
            data = await response.json()
        data = EnqueueTaskSchema(**data)
        self.logger.info(
            'Export task posted', page_id=page_id, recursive=recursive
        )
        return data.task_id

    async def get_task_result(self, task_id: str) -> Optional[TaskResult]:
        """Get result of the selected task.

        :param task_id: task id
        :returns: task result
        """
        attempts_count = 0
        max_attempts_count = self.config.NOTION_GET_TASK_MAX_ATTEMPTS
        while attempts_count < max_attempts_count:
            assert self.session  # mypy
            async with self.session.post(
                self.config.NOTION_GET_TASK_ENDPOINT,
                json={'taskIds': [task_id]},
            ) as response:
                response.raise_for_status()
                data = await response.json()
                self.logger.debug('Got task response', data=data)
                data = TaskResults(**data)
                for task_result in data.results:
                    if (
                        task_result.id == task_id
                        and task_result.state == TaskResult.State.SUCCESS
                    ):
                        return task_result
                pages_exported = -1
                if task_result.status:
                    pages_exported = task_result.status.pages_exported
                self.logger.debug(
                    'Task not ready, retrying',
                    progress=pages_exported,
                    retry_in=self.config.NOTION_GET_TASK_RETRY_TIME,
                    attempts=f'{attempts_count} of {max_attempts_count}',
                )
                await asyncio.sleep(self.config.NOTION_GET_TASK_RETRY_TIME)
                attempts_count += 1
        self.logger.error('Cannot get task result')
        return None

    async def export_page(
        self, page_id: str, destination: Path, recursive: bool = False
    ) -> None:
        """Export a page to a zip-file.

        :param page_id: page id
        :param destination: zip-file destination
        :param recursive: recursive
        """
        # Enqueue task
        task_id = await self.enqueue_export_task(page_id, recursive=recursive)
        # Get task result
        task_result = await self.get_task_result(task_id)
        if task_result:
            self.logger.info(
                'Export complete, downloading file',
                url=task_result.status.export_url,
            )
            assert self.session  # mypy
            async with self.session.get(
                task_result.status.export_url
            ) as response:
                async with aiofiles.open(destination, mode='wb') as f:
                    await f.write(await response.read())


class AnkiClient(BaseClient):
    """Anki client."""

    #: AnkiConnect API version
    API_VERSION: int = 6
    #: Note model stylesheet
    MODEL_CSS: str = (Path(__file__).parent / 'model.css').read_text('utf-8')
    #: Note model name
    MODEL_NAME: str = 'notion-anki-sync'
    #: Card template name
    CARD_TEMPLATE_NAME: str = 'Question-Answer'
    #: Note front side template
    FRONT_TMPL: str = '<div class="front">{{Front}}</div>'
    #: Note back side template
    BACK_TMPL: str = (
        '<div class="front-on-back">{{Front}}</div>'
        '<hr id="back">'
        '<div class="back">{{Back}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )

    def __init__(self, config: Config):
        """Init the client.

        :param config: configuration
        """
        super().__init__(config)

    async def _make_request(self, payload: Dict[str, Any]) -> ResponseSchema:
        """Make POST request with given payload.

        :param payload: a payload
        :returns: response
        """
        # Sometimes AnkiConnect drops connection so we retry here
        while True:
            try:
                assert self.session  # mypy
                async with self.session.post(
                    self.config.ANKICONNECT_ENDPOINT, json=payload
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                    response = ResponseSchema(**json.loads(data))
                    if response.error:
                        response.error = response.error.capitalize()
                    return response
            except ClientOSError as exc:
                retry_in = self.config.ANKI_RETRY_INTERVAL
                self.logger.warning(
                    'Connection error', error=exc.strerror, retry_in=retry_in
                )
                await asyncio.sleep(retry_in)

    async def anki_available(self) -> bool:
        """Check the connection to AnkiConnect endpoint.

        :returns: if the connection is successful
        """
        payload = {
            'action': 'version',
            'version': self.API_VERSION,
        }
        try:
            assert self.session  # mypy
            async with self.session.post(
                self.config.ANKICONNECT_ENDPOINT, json=payload
            ) as resp:
                data = await resp.read()
                response = ResponseSchema(**json.loads(data))
        except ClientConnectorError:
            self.logger.warning(
                'Cannot connect to Anki. Is it running?',
                retry_in=self.config.ANKI_RETRY_INTERVAL,
                endpoint_url=self.config.ANKICONNECT_ENDPOINT,
            )
            return False
        else:
            if response.result != self.API_VERSION:
                self.logger.warning(
                    'Anki API version mismatch',
                    expected=self.API_VERSION,
                    got=response.result,
                )
        return True

    async def ensure_deck(self, name: str) -> None:
        """Ensure a deck with a given name exists.

        :param name: deck name
        """
        payload = {
            'action': 'createDeck',
            'version': self.API_VERSION,
            'params': {'deck': name},
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(name=name)
        if resp.error:
            _logger.info(resp.error)
        else:
            _logger.info('Deck created')

    async def update_note_model_template(self):
        """Update note model template."""
        payload = {
            'action': 'updateModelTemplates',
            'version': self.API_VERSION,
            'params': {
                'model': {
                    'name': self.MODEL_NAME,
                    'templates': {
                        self.CARD_TEMPLATE_NAME: {
                            "Front": self.FRONT_TMPL,
                            "Back": self.BACK_TMPL,
                        }
                    },
                }
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(model_name=self.MODEL_NAME)
        if resp.error:
            _logger.warning(resp.error)
        else:
            _logger.info('Model template updated')

    async def update_note_model_styling(self):
        """Update note model styling."""
        payload = {
            'action': 'updateModelStyling',
            'version': self.API_VERSION,
            'params': {
                'model': {
                    'name': self.MODEL_NAME,
                    'css': self.MODEL_CSS,
                }
            },
        }
        resp = await self._make_request(payload)
        if resp.error:
            self.logger.warning(resp.error)
        else:
            self.logger.info('Model styling updated')

    async def ensure_note_model(self):
        """Ensure note model exists."""
        payload = {
            'action': 'createModel',
            'params': {
                'modelName': self.MODEL_NAME,
                'inOrderFields': ['Front', 'Back', 'Source'],
                'css': self.MODEL_CSS,
                'cardTemplates': [
                    {
                        'Name': self.CARD_TEMPLATE_NAME,
                        'Front': self.FRONT_TMPL,
                        'Back': self.BACK_TMPL,
                    }
                ],
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(model_name=self.MODEL_NAME)
        if resp.error:
            _logger.info(resp.error)
            await self.update_note_model_styling()
            await self.update_note_model_template()
        else:
            _logger.info('Model created')

    async def create_deck_and_model(self, name: str) -> None:
        """Create a deck with a given name and a note model.

        :param name: deck name
        """
        await asyncio.gather(self.ensure_deck(name), self.ensure_note_model())

    async def store_file(self, filename: str, path: Path):
        """Store file with given name.

        :param filename: a filename file will be stored under
        :param path: absolute path to a file
        """
        encoded_file = base64.b64encode(path.read_bytes())
        payload = {
            'action': 'storeMediaFile',
            'version': self.API_VERSION,
            'params': {
                'data': encoded_file.decode('utf-8'),
                'filename': filename,
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(filename=filename)
        if resp.error:
            _logger.warning(resp.error)
        else:
            _logger.info('File stored')

    async def create_note(self, note: Note):
        """Create a note.

        :param note: a note
        """
        if note.images:
            for image in note.images:
                prefix = ''.join(c for c in image.src if c.isalnum())
                filename = f'{prefix}_{image.abs_path.name}'
                await self.store_file(filename=filename, path=image.abs_path)
                note.back = note.back.replace(image.src, filename)
        payload = {
            'action': 'addNote',
            'version': self.API_VERSION,
            'params': {
                'note': {
                    'deckName': self.config.ANKI_TARGET_DECK,
                    'modelName': self.MODEL_NAME,
                    'fields': {
                        'Front': note.front,
                        'Back': note.back,
                        'Source': f'<a href="{note.source}">{note.source}</a>',
                    },
                    'tags': note.tags,
                    'options': {
                        'allowDuplicate': False,
                        'duplicateScope': 'deck',
                    },
                },
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(front=repr(note.front))
        if resp.error:
            _logger.info(resp.error)
        else:
            _logger.info('Note created')

    async def trigger_sync(self) -> None:
        """Trigger Anki sync."""
        payload = {'action': 'sync', 'version': self.API_VERSION}
        resp = await self._make_request(payload)
        if resp.error:
            self.logger.warning(resp.error)
        else:
            self.logger.info('Deck sync triggered')

    async def create_notes(self, notes: List[Note]) -> None:
        """Create multiple notes.

        :param notes: notes
        """
        tasks = [asyncio.create_task(self.create_note(note)) for note in notes]
        await asyncio.gather(*tasks)
