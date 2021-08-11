"""Notion Sync plugin."""
import json
import zipfile
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from traceback import format_exc
from typing import Any, Dict, List, Optional, Set, cast

from anki import Collection
from aqt import mw
from aqt.hooks_gen import main_window_did_init
from aqt.utils import showCritical, showInfo
from jsonschema import ValidationError, validate
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtWidgets import QAction, QMessageBox

from .helpers import (
    BASE_DIR,
    enable_logging_to_file,
    get_logger,
    normalize_block_id,
    safe_path,
)
from .notes_manager import NotesManager
from .notion_client import NotionClient, NotionClientError
from .parser import AnkiNote, extract_notes_data


class NotionSyncPlugin(QObject):
    """Notion sync plugin.

    Reads config, handles signals from Anki and spawns synchronization tasks
    on timer.
    """

    #: Default deck name
    DEFAULT_DECK_NAME: str = 'Notion Sync'
    #: Default sync interval, min
    DEFAULT_SYNC_INTERVAL: int = 30

    def __init__(self):
        """Init plugin."""
        super().__init__()
        # While testing `mw` is None
        if not mw:
            return
        # Load config
        config = mw.addonManager.getConfig(__name__)
        mw.addonManager.setConfigUpdatedAction(__name__, self.reload_config)
        # Validate config
        self.config = self.get_valid_config(config)
        # Create a logger
        self.debug = 'debug' in self.config and self.config['debug']
        if self.debug:
            enable_logging_to_file()
        self.logger = get_logger(self.__class__.__name__, self.debug)
        self.logger.info('Config loaded: %s', self.config)
        # Anki collection and note manager
        self.collection: Optional[Collection] = None
        self._collection_seeded = False
        self.notes_manager: Optional[NotesManager] = None
        # Workers scaffolding
        self.thread_pool = QThreadPool()
        self.synced_note_ids: Set[int] = set()
        self._alive_workers: int = 0
        self._sync_errors: List[str] = []
        # Sync stats
        self._processed = self._created = self._updated = self._deleted = 0
        self.existing_note_ids: Set[int] = set()
        self._remove_obsolete_on_sync = False
        # Add action to Anki menu
        self.notion_menu = None
        self.add_actions()
        # Add callback to seed the collection then it's ready
        main_window_did_init.append(self.seed_collection)
        # Perform auto sync after main window initialization
        main_window_did_init.append(self.auto_sync)
        # Create and run timer
        self._is_auto_sync = True
        self.timer = QTimer()
        sync_interval_milliseconds = (
            self.config.get('sync_every_minutes', self.DEFAULT_SYNC_INTERVAL)
            * 60  # seconds
            * 1000  # milliseconds
        )
        if sync_interval_milliseconds:
            self.timer.setInterval(sync_interval_milliseconds)
            self.timer.timeout.connect(self.auto_sync)
            self.timer.start()

    def _validate_config(self, config: Dict[str, Any]):
        """Validate config.

        :param config: config
        """
        # Load schema and validate configuration
        with open(
            BASE_DIR / 'schemas/config_schema.json', encoding='utf8'
        ) as s:
            schema = json.load(s)
        validate(config, schema)

    def get_valid_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Get valid configuration.

        :param config: configuration
        :returns: either configuration provided (if it's valid) or default
            config
        """
        try:
            self._validate_config(config)
        except ValidationError as exc:
            showCritical(str(exc), title='Notion loader config load error')
            assert mw  # mypy
            default_config = mw.addonManager.addonConfigDefaults(str(BASE_DIR))
            return cast(Dict[str, Any], default_config)
        else:
            return config

    def reload_config(self, new_config: Dict[str, Any]) -> None:
        """Reload configuration.

        :param new_config: new configuration
        """
        try:
            self._validate_config(new_config)
        except ValidationError as exc:
            self.logger.error('Config update error', exc_info=exc)
            showCritical(str(exc), title='Notion loader config update error')
        else:
            self.config = new_config

    def add_actions(self):
        """Add Notion menu entry with actions to Tools menu."""
        assert mw  # mypy
        self.notion_menu = mw.form.menuTools.addMenu('Notion')
        load_action = QAction('Load notes', mw)
        load_action_and_remove_obsolete = QAction(
            'Load notes and remove obsolete', mw
        )
        load_action.triggered.connect(self.sync)
        load_action_and_remove_obsolete.triggered.connect(
            self.sync_and_remove_obsolete
        )
        self.notion_menu.addActions(
            (load_action, load_action_and_remove_obsolete)
        )

    def seed_collection(self):
        """Init collection and note manager after Anki loaded."""
        self.collection = mw.col
        if not self.collection:
            self.logger.error('Collection is empty')
            return
        self.notes_manager = NotesManager(
            collection=self.collection,
            deck_name=self.config.get(
                'anki_target_deck', self.DEFAULT_DECK_NAME
            ),
            debug=self.debug,
        )
        self.logger.info('Collection initialized')
        self.existing_note_ids = self.notes_manager.existing_note_ids
        self._collection_seeded = True

    def handle_worker_result(self, notes: List[AnkiNote]) -> None:
        """Add notes to collection.

        :param notes: notes
        """
        assert self.notes_manager  # mypy
        try:
            for note in notes:
                if not note.front:
                    self.logger.warning(
                        'Note front is empty. Back: %s', note.back
                    )
                    continue
                self._processed += 1
                # Find out if note already exists
                note_id = self.notes_manager.find_note(note)
                if note_id:
                    is_updated = self.notes_manager.update_note(note_id, note)
                    if is_updated:
                        self._updated += 1
                # Create new note
                else:
                    note_id = self.notes_manager.create_note(note)
                    self._created += 1
                self.synced_note_ids.add(note_id)
        except Exception:
            error_msg = format_exc()
            self._sync_errors.append(error_msg)

    def handle_sync_finished(self) -> None:
        """Handle sync finished.

        In case of any error - show error message in manual mode and do nothing
        otherwise.  If no error - save the collection and show sync statistics
        in manual mode.  If `self._remove_obsolete_on_sync` is True - remove
        all notes that is not added or updated in current sync.
        """
        assert self.notes_manager  # mypy
        assert self.collection  # mypy
        self._alive_workers -= 1
        if self._alive_workers:
            return
        self.notion_menu.setTitle('Notion')
        # Show errors if manual sync
        if self._sync_errors:
            if not self._is_auto_sync:
                error_msg = '\n'.join(self._sync_errors)
                showCritical(error_msg, title='Loading from Notion failed')
        # If no errors - save collection and refresh Anki window
        else:
            if self._remove_obsolete_on_sync:
                ids_to_remove = self.existing_note_ids - self.synced_note_ids
                if ids_to_remove:
                    msg = (
                        f'Will delete {len(ids_to_remove)} obsolete note(s), '
                        f'continue?'
                    )
                    do_delete = QMessageBox.question(
                        mw,
                        'Confirm deletion',
                        msg,
                        QMessageBox.Yes | QMessageBox.No,  # type: ignore
                    )
                    if do_delete == QMessageBox.Yes:
                        self.notes_manager.remove_notes(ids_to_remove)
                        self._deleted += len(ids_to_remove)
            self.collection.save(trx=False)
            mw.maybeReset()  # type: ignore[union-attr]
            mw.deckBrowser.refresh()  # type: ignore[union-attr]
            stats = (
                f'Processed: {self._processed}\n'
                f'Created: {self._created}\n'
                f'Updated: {self._updated}\n'
                f'Deleted: {self._deleted}'
            )
            if not self._is_auto_sync:
                showInfo(
                    f'Successfully loaded:\n{stats}',
                    title='Loading from Notion',
                )
        self.logger.info(
            'Sync finished, processed=%s, created=%s, updated=%s, deleted=%s',
            self._processed,
            self._created,
            self._updated,
            self._deleted,
        )
        self._reset_stats()

    def handle_worker_error(self, error_message) -> None:
        """Handle worker error.

        :param error_message: error message
        """
        self._sync_errors.append(error_message)

    def auto_sync(self) -> None:
        """Perform synchronization in background."""
        self.logger.info('Auto sync started')
        # Reload config
        assert mw  # mypy
        self.config = mw.addonManager.getConfig(__name__)
        self._is_auto_sync = True
        self._sync()

    def sync(self) -> None:
        """Perform synchronization and report result."""
        self.logger.info('Sync started')
        # Reload config
        assert mw  # mypy
        self.config = mw.addonManager.getConfig(__name__)
        if not self._alive_workers:
            self._is_auto_sync = False
            self._sync()
        else:
            showInfo(
                'Sync is already in progress, please wait',
                title='Load from Notion',
            )

    def sync_and_remove_obsolete(self) -> None:
        """Perform synchronization and remove obsolete notes."""
        self.logger.info('Sync with remove obsolete started')
        self._remove_obsolete_on_sync = True
        self.sync()

    def _reset_stats(self) -> None:
        """Reset variables before sync.

        Saves pre-sync existing note ids and resets sync stats and errors.
        """
        self._remove_obsolete_on_sync = False
        self.synced_note_ids.clear()
        assert self.notes_manager  # mypy
        self.existing_note_ids = self.notes_manager.existing_note_ids
        self._processed = self._created = self._updated = self._deleted = 0
        self._sync_errors = []

    def _sync(self) -> None:
        """Start sync."""
        if not self.collection or not self.notes_manager:
            self.logger.warning('Collection is not initialized yet')
            return
        # If collection is not seeded - seed it
        if not self._collection_seeded:
            self.seed_collection()
        self.notion_menu.setTitle('Notion (syncing...)')
        for page_spec in self.config.get('notion_pages', []):
            page_id, recursive = page_spec['page_id'], page_spec['recursive']
            page_id = normalize_block_id(page_id)
            worker = NotesExtractorWorker(
                notion_token=self.config['notion_token'],
                page_id=page_id,
                recursive=recursive,
                notion_namespace=self.config.get('notion_namespace', ''),
                debug=self.debug,
            )
            worker.signals.result.connect(self.handle_worker_result)
            worker.signals.error.connect(self.handle_worker_error)
            worker.signals.finished.connect(self.handle_sync_finished)
            # Start worker
            self.thread_pool.start(worker)
            self._alive_workers += 1


class NoteExtractorSignals(QObject):
    """The signals available from a running extractor thread."""

    #: Extraction finished
    finished = pyqtSignal()
    #: Notes data
    result = pyqtSignal(object)
    #: Error
    error = pyqtSignal(str)


class NotesExtractorWorker(QRunnable):
    """Notes extractor worker thread."""

    def __init__(
        self,
        notion_token: str,
        page_id: str,
        recursive: bool,
        notion_namespace: str,
        debug: bool = False,
    ):
        """Init notes extractor.

        :param notion_token: Notion token
        :param page_id: Notion page id
        :param recursive: recursive export
        :param notion_namespace: Notion namespace to form source links
        :param debug: debug log level
        """
        super().__init__()
        self.debug = debug
        self.logger = get_logger(f'worker_{page_id}', self.debug)
        self.signals = NoteExtractorSignals()
        self.notion_token = notion_token
        self.page_id = page_id
        self.recursive = recursive
        self.notion_namespace = notion_namespace

    def run(self) -> None:
        """Extract note data from given Notion page.

        Export Notion page as HTML, extract notes data from the HTML and send
        results.
        """
        self.logger.info('Worker started')
        try:
            with TemporaryDirectory() as tmp_dir:
                # Export given Notion page as HTML
                tmp_path = safe_path(Path(tmp_dir))
                export_path = tmp_path / f'{self.page_id}.zip'
                client = NotionClient(self.notion_token, self.debug)
                client.export_page(
                    page_id=self.page_id,
                    destination=export_path,
                    recursive=self.recursive,
                )
                self.logger.info(
                    'Exported file downloaded: path=%s', str(export_path)
                )
                # Extract notes data from the HTML files
                with zipfile.ZipFile(export_path) as zip_file:
                    zip_file.extractall(tmp_path)
                notes = []
                for html_path in tmp_path.rglob('*.html'):
                    notes += extract_notes_data(
                        source=Path(html_path),
                        notion_namespace=self.notion_namespace,
                        debug=self.debug,
                    )
                self.logger.info('Notes extracted: count=%s', len(notes))
        except NotionClientError as exc:
            self.logger.error('Error extracting notes', exc_info=exc)
            error_msg = f'Cannot export {self.page_id}:\n{exc}'
            self.signals.error.emit(error_msg)
        except OSError as exc:  # Long path
            self.logger.warning('Error deleting files', exc_info=exc)
            # Delete manually
            rmtree(tmp_path, ignore_errors=True)
        else:
            self.signals.result.emit(notes)
        finally:
            self.signals.finished.emit()


NotionSyncPlugin()
