"""Notion Sync plugin."""
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Set

from aqt import mw
from aqt.hooks_gen import main_window_did_init
from aqt.utils import showWarning
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtWidgets import QAction

from .helpers import get_logger, normalize_block_id
from .notes_manager import NotesManager
from .notion_client import NotionClient
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
        # Load config
        self.config = mw.addonManager.getConfig(__name__)
        # Create a logger
        self.debug = 'debug' in self.config and self.config['debug']
        self.logger = get_logger(self.__class__.__name__, self.debug)
        self.logger.info('Config loaded: %s', self.config)
        # Anki collection and note manager
        self.collection = None
        self.notes_manager = None
        # Workers scaffolding
        self.thread_pool = QThreadPool()
        self._note_ids: Set[int] = set()
        self._alive_workers: int = 0
        self._worker_error: bool = False
        # Add action to Anki menu
        self.add_action()
        # Add callback to seed the collection then it's ready
        main_window_did_init.append(self.seed_collection)
        # Create and run timer
        self.timer = QTimer()
        timeout_milliseconds = (
            self.config.get('sync_every_minutes', self.DEFAULT_SYNC_INTERVAL)
            * 60  # seconds
            * 1000  # milliseconds
        )
        self.timer.setInterval(timeout_milliseconds)
        self.timer.timeout.connect(self.sync)
        self.timer.start()

    def add_action(self):
        """Add "Load from Notion" action to Tools menu."""
        assert mw  # mypy
        action = QAction('Load from Notion', mw)
        mw.form.menuTools.addAction(action)
        action.triggered.connect(self.sync)

    def seed_collection(self):
        """Init collection and note manager after Anki loaded."""
        self.collection = mw.col
        if not self.collection:
            self.logger.error('Collection is empty')
        self.notes_manager = NotesManager(
            collection=self.collection,
            deck_name=self.config.get(
                'anki_target_deck', self.DEFAULT_DECK_NAME
            ),
            debug=self.debug,
        )
        self.logger.info('Collection initialized')
        self.sync()

    def add_notes(self, notes: List[AnkiNote]) -> None:
        """Add notes to collection.

        :param notes: notes
        """
        for note in notes:
            id_ = self.notes_manager.upsert_note(note)
            self._note_ids.add(id_)

    def remove_obsolete_notes(self) -> None:
        """Remove obsolete notes after all workers are finished."""
        self._alive_workers -= 1
        if self._alive_workers:
            return
        if not self._worker_error:
            self.notes_manager.remove_all_notes_excluding(self._note_ids)
            self._note_ids.clear()
        self.collection.save()
        mw.maybeReset()  # type: ignore[union-attr]
        mw.deckBrowser.refresh()  # type: ignore[union-attr]
        self.logger.info('Sync finished')

    def handle_worker_error(self) -> None:
        """Handle worker error."""
        self._alive_workers -= 1
        self._worker_error = True

    def sync(self):
        """Start sync."""
        self.logger.info('Sync triggered')
        self._worker_error = False
        if not self.collection or not self.notes_manager:
            self.logger.warning('Collection is not initialized yet')
            return
        for page_spec in self.config.get('notion_pages', []):
            if 'notion_token' not in self.config:
                showWarning(
                    'Please provide "notion_token" in plugin config',
                    title='Notion Sync plugin error',
                )
            page_id, recursive = page_spec['page_id'], page_spec['recursive']
            page_id = normalize_block_id(page_id)
            worker = NotesExtractorWorker(
                notion_token=self.config['notion_token'],
                page_id=page_id,
                recursive=recursive,
                notion_namespace=self.config.get('notion_namespace', ''),
            )
            worker.signals.result.connect(self.add_notes)
            worker.signals.finished.connect(self.remove_obsolete_notes)
            worker.signals.error.connect(self.handle_worker_error)
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
    error = pyqtSignal()


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
        self.logger.info('Sync started')
        # Export Notion page with givenDownload e
        try:
            with TemporaryDirectory() as tmp_dir:
                # Export given Notion page as HTML
                tmp_path = Path(tmp_dir)
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
                        source=html_path,
                        notion_namespace=self.notion_namespace,
                        debug=self.debug,
                    )
                self.logger.info('Notes extracted: count=%s', len(notes))
        except Exception as exc:
            self.logger.error('Error extracting notes', exc_info=exc)
            self.signals.error.emit()
        else:
            self.signals.result.emit(notes)
        finally:
            self.signals.finished.emit()


NotionSyncPlugin()
