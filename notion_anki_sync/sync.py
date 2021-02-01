"""Syncing."""
import asyncio
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import dateutil.tz
import structlog

from notion_anki_sync.clients import AnkiClient, NotionClient
from notion_anki_sync.config import Config
from notion_anki_sync.parser import extract_notes_data

# Configure logging
config = Config()
config.init_logging()
#: Logger
logger = structlog.getLogger('sync')
#: Block id regex
BLOCK_ID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)


def normalize_block_id(block_id: str) -> str:
    """Normalize Notion block id.

    I.e. 'd151217ae85f4e79a05406f7db2bb0da'
    -> 'd151217a-e85f-4e79-a054-06f7db2bb0da'

    :param block_id: Notion block id
    :returns: normalized block id
    """
    if not BLOCK_ID_RE.match(block_id):
        return (
            f'{block_id[:8]}-{block_id[8:12]}-{block_id[12:16]}-'
            f'{block_id[16:20]}-{block_id[20:]}'
        )
    return block_id


async def sync_page(
    page_id: str,
    recursive: bool,
    config: Config,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Sync a single Notion page.

    :param page_id: page id
    :param recursive: recursive export
    :param config: configuration
    :param logger: logger
    """
    logger.info('Sync started')
    with TemporaryDirectory() as tmp_dir:
        # Export given Notion page as HTML
        tmp_path = Path(tmp_dir)
        export_path = tmp_path / f'{page_id}.zip'
        async with NotionClient(config) as notion_client:
            await notion_client.export_page(
                page_id=page_id,
                destination=export_path,
                recursive=recursive,
            )
        logger.info('Exported file downloaded', path=str(export_path))
        # Extract notes data from the HTML
        with zipfile.ZipFile(export_path) as zip_file:
            zip_file.extractall(tmp_path)
        notes = []
        for html_path in tmp_path.rglob('*.html'):
            notes += extract_notes_data(html_path, config.NOTION_NAMESPACE)
        logger.info('Notes extracted', count=len(notes))
        # Create Anki notes
        if notes:
            async with AnkiClient(config) as anki_client:
                await anki_client.create_notes(notes)


async def sync():
    """Sync Notion to Anki."""
    while True:
        config = Config()
        async with AnkiClient(config) as anki_client:
            while not await anki_client.anki_available():
                await asyncio.sleep(config.ANKI_RETRY_INTERVAL)
            # Create deck and models
            async with AnkiClient(config) as anki_client:
                await anki_client.create_deck_and_model(
                    config.ANKI_TARGET_DECK
                )
        tasks = []
        for page_spec in config.NOTION_TARGET_PAGES:
            page_id, recursive = page_spec.page_id, page_spec.recursive
            page_id = normalize_block_id(page_id)
            _logger = logger.bind(page_id=page_id, recursive=recursive)
            tasks.append(sync_page(page_id, recursive, config, _logger))
        await asyncio.gather(*tasks)
        async with AnkiClient(config) as anki_client:
            await anki_client.trigger_sync()
        next_due = datetime.now(tz=dateutil.tz.UTC) + timedelta(
            seconds=config.SYNC_EVERY
        )
        logger.info('Sync complete', next_due=next_due.isoformat())
        await asyncio.sleep(config.SYNC_EVERY)


def main():
    """Run sync forever."""
    try:
        asyncio.run(sync())
    except KeyboardInterrupt:
        logger.info('Exiting...')


if __name__ == '__main__':
    main()
