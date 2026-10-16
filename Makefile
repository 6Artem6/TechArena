PYTHON   := python3
SRC_DIRS := src

.PHONY: install format lint check fix commit

## Установка dev-зависимостей
install:
	$(PYTHON) -m pip install -r requirements-dev.txt

## Форматирование: isort + black (изменяют файлы)
format:
	$(PYTHON) -m isort $(SRC_DIRS)
	$(PYTHON) -m black $(SRC_DIRS)

## Проверка типов: mypy (только читает)
lint:
	$(PYTHON) -m mypy $(SRC_DIRS)

## Проверка без изменений (для CI)
check:
	$(PYTHON) -m isort --check-only $(SRC_DIRS)
	$(PYTHON) -m black --check $(SRC_DIRS)
	$(PYTHON) -m mypy $(SRC_DIRS)

## Исправить + проверить (вызывается хуком pre-commit)
fix: format lint

## Установить git-хук pre-commit
install-hooks:
	cp hooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Хук pre-commit установлен."
