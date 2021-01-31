.PHONY: \
	fmt \
	lint

all:
	@echo "fmt                 Format code."
	@echo "lint                Lint code."

FILES = notion_anki_sync

fmt:
	poetry run black $(FILES)
	poetry run isort $(FILES)

lint:
	poetry run black --check $(FILES)
	poetry run isort --check-only $(FILES)
	poetry run flake8 $(FILES)
	poetry run pydocstyle $(FILES)
	poetry run mypy --sqlite-cache $(FILES)
