"""Notion and Anki clients."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import TracebackType
from typing import Any, Dict, List, Optional, Type, TypeVar

import aiofiles
import aiohttp
import structlog
from aiohttp import (
    ClientConnectorError,
    ClientOSError,
    ServerDisconnectedError,
)

from notion_anki_sync.config import Config
from notion_anki_sync.exceptions import AnkiError
from notion_anki_sync.models.anki import AnkiNote, AnkiResponseSchema
from notion_anki_sync.models.notion import (
    EnqueueTaskSchema,
    TaskResult,
    TaskResults,
)

BT = TypeVar('BT', bound='BaseClient')


class BaseClient:
    """Basic aiohttp-based client."""

    def __init__(self, config: Config) -> None:
        """Init the client.

        :param config: configuration
        """
        self.logger = structlog.getLogger(f'sync.{self.__class__.__name__}')
        self.config = config
        self.cookies: Optional[Dict[str, str]] = None
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self: BT) -> BT:
        """Create a session and return the client."""
        timeout = aiohttp.ClientTimeout(total=5)
        conn = aiohttp.TCPConnector(limit=5)
        self.session = aiohttp.ClientSession(
            cookies=self.cookies, connector=conn, timeout=timeout
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

    def __init__(self, config: Config) -> None:
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
        while True:
            async with self.session.post(
                self.config.NOTION_ENQUEUE_TASK_ENDPOINT, json=payload
            ) as response:
                if response.status == 401:
                    self.logger.error('Invalid token')
                    response.raise_for_status()
                elif response.status >= 500:
                    self.logger.error(
                        'Notion server error, retrying',
                        retry_in=self.config.NOTION_RETRY_TIME,
                    )
                else:
                    data = await response.json()
                    break
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
                    retry_in=self.config.NOTION_RETRY_TIME,
                    attempts=f'{attempts_count} of {max_attempts_count}',
                )
                await asyncio.sleep(self.config.NOTION_RETRY_TIME)
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
            assert task_result.status  # mypy
            self.logger.info(
                'Export complete, downloading file',
                url=task_result.status.export_url,
            )
            assert self.session  # mypy
            assert task_result.status.export_url  # mypy
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
    #: Cloze note front side template
    CLOZE_FRONT_TMPL: str = '<div class="front">{{cloze:Front}}</div>'
    #: Cloze note back side template
    CLOZE_BACK_TMPL: str = (
        '<div class="front">{{cloze:Front}}</div>'
        '<hr id="source">'
        '<div class="backlink">{{Source}}</div>'
    )
    #: Connection exceptions
    API_EXCEPTIONS = (
        ClientOSError,
        ConnectionError,
        ClientConnectorError,
        ServerDisconnectedError,
        asyncio.TimeoutError,
    )

    def __init__(self, config: Config) -> None:
        """Init the client.

        :param config: configuration
        """
        super().__init__(config)

    async def _make_request(
        self, payload: Dict[str, Any]
    ) -> AnkiResponseSchema:
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
                    response = AnkiResponseSchema(**json.loads(data))
                    if response.error_message:
                        response.error_message = (
                            response.error_message.capitalize()
                        )
                        response.error = AnkiError(response.error)
                    return response
            except self.API_EXCEPTIONS as exc:
                retry_in = self.config.ANKI_RETRY_INTERVAL
                error_string = getattr(exc, 'strerror', '')
                self.logger.warning(
                    'Connection error', error=error_string, retry_in=retry_in
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
                response = AnkiResponseSchema(**json.loads(data))
        except self.API_EXCEPTIONS:
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
        await self._make_request(payload)
        self.logger.info('Deck created', name=name)

    async def update_note_model_template(
        self, model_name: str, template_name: str, fields: Dict[str, str]
    ) -> None:
        """Update note model template.

        :param model_name: model name to update
        :param template_name: template name to update
        :param fields: templates for fields
        """
        payload = {
            'action': 'updateModelTemplates',
            'version': self.API_VERSION,
            'params': {
                'model': {
                    'name': model_name,
                    'templates': {template_name: fields},
                }
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(
            model_name=model_name, template=template_name, fields=fields
        )
        if resp.error_message:
            _logger.warning(resp.error_message)
        else:
            _logger.info('Model template updated')

    async def update_note_model_styling(self, model_name: str):
        """Update note model styling.

        :param model_name: name of a model
        """
        payload = {
            'action': 'updateModelStyling',
            'version': self.API_VERSION,
            'params': {
                'model': {
                    'name': model_name,
                    'css': self.MODEL_CSS,
                }
            },
        }
        resp = await self._make_request(payload)
        _logger = self.logger.bind(model_name=model_name)
        if resp.error_message:
            _logger.warning(resp.error_message)
        else:
            _logger.info('Model styling updated')

    async def ensure_note_models(self):
        """Ensure question-answer and cloze note models exists and have
        appropriate styling and templates.
        """
        # Try to create a question-answer model
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
        # If model exists - update styling and templates
        if resp.error == AnkiError.MODEL_EXISTS:
            await self.update_note_model_styling(self.MODEL_NAME)
            template_fields = {
                'Front': self.FRONT_TMPL,
                'Back': self.BACK_TMPL,
            }
            await self.update_note_model_template(
                self.MODEL_NAME, self.CARD_TEMPLATE_NAME, template_fields
            )
        elif resp.error == AnkiError.UNKNOWN:
            self.logger.error(
                'Error creating model',
                model_name=self.MODEL_NAME,
                error=resp.error_message,
            )
        else:
            self.logger.info('Model created', model_name=self.MODEL_NAME)
        # Update styling of cloze model
        cloze_model_name = self.config.ANKI_CLOZE_MODEL
        await self.update_note_model_styling(cloze_model_name)
        # Update templates of cloze model
        template_fields = {
            'Front': self.CLOZE_FRONT_TMPL,
            'Back': self.CLOZE_BACK_TMPL,
        }
        await self.update_note_model_template(
            cloze_model_name, 'Cloze', template_fields
        )

    async def create_deck_and_model(self, name: str) -> None:
        """Create a deck with a given name and a note model.

        :param name: deck name
        """
        await asyncio.gather(self.ensure_deck(name), self.ensure_note_models())

    async def store_file(self, filename: str, path: Path) -> None:
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
        await self._make_request(payload)
        _logger = self.logger.bind(filename=filename)
        _logger.info('File stored')

    async def get_note_id_by_front_text(self, front: str) -> Optional[int]:
        """Get note id by its front content.

        :param front: front content
        :returns: note id
        """
        query = f'deck:"{self.config.ANKI_TARGET_DECK}" front:"{front}"'
        payload = {
            'action': 'findNotes',
            'version': self.API_VERSION,
            'params': {
                'query': query,
            },
        }
        resp = await self._make_request(payload)
        if not resp.result:
            self.logger.error('No cards found', query=query)
            return None
        else:
            if isinstance(resp.result, list):
                if len(resp.result) > 1:
                    self.logger.error('More than 1 note found', query=query)
                    return None
                return resp.result[0]
            return None

    async def upsert_note(self, note: AnkiNote) -> None:
        """Create or update a note.

        :param note: a note
        """
        if note.images:
            for image in note.images:
                await self.store_file(
                    filename=image.filename, path=image.abs_path
                )
        if not note.back:
            model_name = self.config.ANKI_CLOZE_MODEL
            fields = {'Front': note.front, 'Source': note.source}
        else:
            model_name = self.MODEL_NAME
            fields = {
                'Front': note.front,
                'Back': note.back,
                'Source': f'<a href="{note.source}">{note.source}</a>',
            }
        payload = {
            'action': 'addNote',
            'version': self.API_VERSION,
            'params': {
                'note': {
                    'deckName': self.config.ANKI_TARGET_DECK,
                    'modelName': model_name,
                    'fields': fields,
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
        # Update note if duplicate
        if resp.error == AnkiError.DUPLICATE_NOTE:
            note_id = await self.get_note_id_by_front_text(note.front)
            if note_id:
                payload = {
                    'action': 'updateNoteFields',
                    'version': self.API_VERSION,
                    'params': {
                        'note': {
                            'id': note_id,
                            'fields': {'Back': note.back},
                        }
                    },
                }
                resp = await self._make_request(payload)
                if resp.error_message:
                    _logger.error(
                        'Cannot update note', error=resp.error_message
                    )
                else:
                    _logger.info('Note updated')
        elif not resp.error:
            _logger.info('Note created')
        else:
            _logger.error('Cannot upsert note', error=resp.error_message)

    async def add_notes(self, notes: List[AnkiNote]) -> None:
        """Create multiple notes.

        :param notes: notes
        """
        tasks = [asyncio.create_task(self.upsert_note(note)) for note in notes]
        await asyncio.gather(*tasks)

    async def trigger_sync(self) -> None:
        """Trigger Anki sync."""
        payload = {'action': 'sync', 'version': self.API_VERSION}
        resp = await self._make_request(payload)
        if resp.error_message:
            self.logger.warning(resp.error_message)
        else:
            self.logger.info('Deck sync triggered')
