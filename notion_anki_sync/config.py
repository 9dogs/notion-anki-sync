"""Application config."""
import logging
import logging.config
from typing import List, Optional

import structlog
from pydantic import BaseModel, BaseSettings


class PageSpec(BaseModel):
    """Notion target page spec."""

    #: Page id
    page_id: str
    #: Recursive export (include subpages)
    recursive: Optional[bool] = False


class Config(BaseSettings):
    """Application config."""

    #: Debug
    DEBUG = False
    #: Sync every, sec
    SYNC_EVERY: int = 3600  # 1 hour
    #: Delay for retrying after a failed sync
    RETRY_AFTER_FAIL_DELAY = 20
    #: Notion token
    NOTION_TOKEN: str
    #: Notion namespace (your Notion username)
    NOTION_NAMESPACE: str
    #: Notion page ids to sync and 'recursive' flags
    NOTION_TARGET_PAGES: List[PageSpec]
    #: Notion enqueue task endpoint
    NOTION_ENQUEUE_TASK_ENDPOINT: str = (
        'https://www.notion.so/api/v3/enqueueTask'
    )
    #: Notion API get task endpoint
    NOTION_GET_TASK_ENDPOINT: str = 'https://www.notion.so/api/v3/getTasks'
    #: Maximum attempts to get a task (increase for larger tasks)
    NOTION_GET_TASK_MAX_ATTEMPTS: int = 600
    #: Notion API retry time, sec
    NOTION_GET_TASK_RETRY_TIME: int = 1
    #: AnkiConnect endpoint
    ANKICONNECT_ENDPOINT: str = 'http://localhost:8765'
    #: Deck to create notes into
    ANKI_TARGET_DECK: str = 'Notion Sync'

    #: Logging configuration
    LOG_CONFIG = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {},
        },
        'handlers': {
            'stdout': {
                'class': 'logging.StreamHandler',
                'level': 'DEBUG',
                'formatter': 'default',
                'stream': 'ext://sys.stdout',
            },
        },
        'loggers': {
            'sync': {'handlers': ['stdout'], 'level': 'INFO'},
            # asyncio warnings
            'asyncio': {'handlers': ['stdout'], 'level': 'WARNING'},
        },
    }

    def init_logging(self):
        """Init logging."""
        if self.DEBUG:
            self.LOG_CONFIG['loggers']['sync']['level'] = 'DEBUG'
        logging.config.dictConfig(self.LOG_CONFIG)
        structlog.configure(
            processors=[
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt='%Y-%m-%dT%H:%M:%S'),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.UnicodeDecoder(),
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(pad_event=50),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    class Config:
        """Meta-configuration."""

        #: Environment file
        env_file = '.env'
        #: Prefix of variables in the environment file
        env_prefix = 'NOTION_ANKI_SYNC_'
        #: Environment file encoding
        env_file_encoding = 'utf-8'
