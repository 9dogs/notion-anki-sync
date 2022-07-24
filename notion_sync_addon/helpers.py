"""Helper functions."""
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

#: Plugin base dir
BASE_DIR = Path(__file__).parent
#: Block id regex
BLOCK_ID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)


def safe_path(original_path: Path) -> Path:
    """Get WinAPI path compatible with long paths if on Windows.

    :param original_path: original path
    :returns: WinAPI path on Windows or original path otherwise
    """
    if os.name != 'nt':
        return original_path
    path = str(original_path.absolute())
    if path.startswith('\\\\'):
        path = f'\\\\?\\UNC\\{path[2:]}'
    else:
        path = f'\\\\?\\{path}'
    return Path(path)


def enable_logging_to_file() -> None:
    """Enable logging to file."""
    root_logger = logging.getLogger('notion_sync')
    handler = logging.FileHandler(
        BASE_DIR / 'log.txt', mode='w', encoding='utf8'
    )
    formatter = logging.Formatter(
        fmt=(
            '%(asctime)s - %(name)s - %(filename)s:%(lineno)d - '
            '%(levelname)s - %(message)s'
        )
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


def get_logger(name: str, debug: bool = False) -> logging.Logger:
    """Create logger with proper handler and formatter.

    :param name: logger name
    :param debug: DEBUG logging level
    :returns: logger
    """
    logger = logging.getLogger(f'notion_sync.{name}')
    null_handler = logging.NullHandler()
    logger.addHandler(null_handler)
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    return logger


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


def safe_str(string: Optional[str]) -> str:
    """Get safe string for logging with system encoding.

    :param string: string to be sanitized
    :returns: sanitized string
    """
    if not string:
        return ''
    encoding = sys.getdefaultencoding()
    return string.encode(encoding, errors='backslashreplace').decode(encoding)
