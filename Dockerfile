FROM python:3.9.1-slim-buster

LABEL maintainer="Mikhail.Knyazev@phystech.edu"
LABEL description="Sync Notion toggle lists to Anki cards."

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

ARG poetry_args='--no-dev'

# Install & config poetry
RUN pip install poetry \
    && poetry config virtualenvs.create true \
    && poetry config virtualenvs.in-project true

# Install project dependencies
COPY poetry.lock pyproject.toml /opt/notion-anki-sync/
WORKDIR /opt/notion-anki-sync
RUN poetry install --no-interaction --no-ansi $poetry_args

COPY . .
ENV PYTHONPATH "${PYTHONPATH}:/opt/notion-anki-sync"

ENTRYPOINT ["poetry", "run"]
CMD ["sync"]
