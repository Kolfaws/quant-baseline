# =============================================================================
# Quant Baseline — 常用命令
#
# 本项目是纯计算组件：不建表、不抓行情。
# 默认使用 uv；若已激活自己的 Python 环境，可用 make RUN= test。
# =============================================================================

.DEFAULT_GOAL := help

UV ?= uv
RUN ?= $(UV) run
PYTHON ?= $(RUN) python
PYTEST ?= $(UV) run --with pytest python -m pytest
RUFF ?= $(UV) run --with ruff ruff

PROFILE ?=
BENCHMARK ?=
START ?=
END ?=
CHUNK_DAYS ?= 365
DATE ?=
PYTEST_ARGS ?=

.PHONY: help install install-pip hooks lint format test test-cov pre-commit \
        check sync backfill build clean

help:
	@echo "Quant Baseline"
	@echo ""
	@echo "用法: make TARGET [PROFILE=...] [BENCHMARK=...] [START=...] [END=...]"
	@echo ""
	@echo "环境:"
	@echo "  install        安装运行与开发依赖 (uv sync --extra dev)"
	@echo "  install-pip    使用当前环境可编辑安装 (pip install -e .[dev])"
	@echo "  hooks          安装 git 钩子"
	@echo "  build          构建 sdist + wheel (uv build)"
	@echo ""
	@echo "质量:"
	@echo "  lint           ruff check"
	@echo "  format         ruff format"
	@echo "  test           运行全部测试"
	@echo "  test-cov       运行测试并输出覆盖率"
	@echo "  pre-commit     运行全部 pre-commit 钩子"
	@echo ""
	@echo "数据:"
	@echo "  check          只读检查 market / backtest 两库结构"
	@echo "  sync           增量同步基准（首次自动全量）"
	@echo "  backfill       历史回补（需 START/END）"
	@echo "  clean          清理缓存与构建产物"

install:
	$(UV) sync

install-pip:
	python -m pip install -e ".[dev]"

hooks:
	$(PYTHON) -m pre_commit install

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

test:
	$(PYTEST) $(PYTEST_ARGS)

test-cov:
	$(UV) run --with pytest --with pytest-cov python -m pytest --cov=quant_baseline --cov-report=term-missing $(PYTEST_ARGS)

pre-commit:
	$(PYTHON) -m pre_commit run --all-files

check:
	$(PYTHON) scripts/check_connections.py $(if $(PROFILE),--profile $(PROFILE),)

sync:
	$(PYTHON) scripts/sync_baseline.py $(if $(PROFILE),--profile $(PROFILE),) \
		$(if $(BENCHMARK),--benchmark $(BENCHMARK),) $(if $(DATE),--date $(DATE),)

backfill:
	@test -n "$(START)" || (echo "用法: make backfill START=2015-01-05 END=2026-11-06" && exit 2)
	$(PYTHON) scripts/backfill_history.py $(if $(PROFILE),--profile $(PROFILE),) \
		--start $(START) $(if $(END),--end $(END),) --chunk-days $(CHUNK_DAYS) \
		$(if $(BENCHMARK),--benchmark $(BENCHMARK),--all)

build:
	$(UV) build

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for r in (pathlib.Path('src'), pathlib.Path('scripts'), pathlib.Path('tests')) if r.exists() for p in r.rglob('__pycache__')]"
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True) for p in map(pathlib.Path, ('.pytest_cache','.ruff_cache','.mypy_cache','build','dist','htmlcov','.coverage'))]"
