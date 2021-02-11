.PHONY: \
	fmt \
	lint

all:
	@echo "fmt                 Format code."
	@echo "lint                Lint code."

FILES = notion_sync_addon tests

fmt:
	poetry run black $(FILES)
	poetry run isort $(FILES)

lint:
	poetry run black --check $(FILES)
	poetry run isort --check-only $(FILES)
	poetry run flake8 $(FILES)
	poetry run pydocstyle $(FILES)
	poetry run mypy --show-error-codes --sqlite-cache $(FILES)

TEST_OUTPUT ?= .
test:
	poetry run py.test \
        --cov notion_anki_sync \
        --cov-report term-missing \
        --cov-report html:$(TEST_OUTPUT)/htmlcov \
        --cov-report xml:$(TEST_OUTPUT)/coverage.xml \
        --junit-xml $(TEST_OUTPUT)/junit.xml \
        tests
