# =============================================================================
# Quant Baseline — 常用开发与数据仓库命令
#
# 默认使用 uv（项目由 uv 初始化，见 .python-version / pyproject.toml）：
#   make install
#   make test
#
# 若已激活自己的 Python 环境（如 conda），可关闭 uv 运行器：
#   make RUN= test
# =============================================================================

.DEFAULT_GOAL := help

UV ?= uv
RUN ?= $(UV) run --extra dev
PYTHON ?= $(RUN) python
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff

# 常用可覆盖参数
PROFILE ?= local
BENCHMARK ?=
START ?=
END ?=
CHUNK_DAYS ?= 365
DATE ?=
PYTEST_ARGS ?=

.PHONY: help install install-pip hooks lint format test test-cov pre-commit \
        init check sync backfill build clean

help:
	@echo "Quant Baseline"
	@echo ""
	@echo "用法: make TARGET [PROFILE=...] [BENCHMARK=...] [START=...] [END=...]"
	@echo ""
	@echo "环境:"
	@echo "  install        安装运行与开发依赖 (uv sync --extra dev)"
	@echo "  install-pip    使用当前环境可编辑安装 (pip install -e .[dev])"
	@echo "  hooks          安装 git 钩子（首次克隆必做）"
	@echo "  build          构建 sdist + wheel (uv build)"
	@echo ""
	@echo "质量:"
	@echo "  lint           ruff check"
	@echo "  format         ruff format（会修改文件）"
	@echo "  test           运行全部测试"
	@echo "  test-cov       运行测试并输出覆盖率"
	@echo "  pre-commit     运行全部 pre-commit 钩子"
	@echo ""
	@echo "数据仓库:"
	@echo "  init           初始化 warehouse 表结构"
	@echo "  check          校验 warehouse 结构"
	@echo "  sync           增量同步基准数据"
	@echo "  backfill       历史回补（需指定 START/END）"
	@echo ""
	@echo "  clean          清理缓存与构建产物"
	@echo ""
	@echo "当前: PROFILE=$(PROFILE) CHUNK_DAYS=$(CHUNK_DAYS)"

install:
	$(UV) sync --extra dev

install-pip:
	python -m pip install -e ".[dev]"

# 把 .pre-commit-config.yaml 落到 .git/hooks/pre-commit（新克隆后必跑一次）
hooks:
	$(PYTHON) -m pre_commit install

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

test:
	$(PYTEST) $(PYTEST_ARGS)

test-cov:
	$(PYTEST) --cov=quant_baseline --cov-report=term-missing $(PYTEST_ARGS)

pre-commit:
	$(PYTHON) -m pre_commit run --all-files

init:
	$(PYTHON) scripts/init_warehouse.py --profile $(PROFILE)

check:
	$(PYTHON) scripts/init_warehouse.py --profile $(PROFILE) --check

sync:
	$(PYTHON) scripts/sync_baseline.py --profile $(PROFILE) $(if $(BENCHMARK),--benchmark $(BENCHMARK),) $(if $(DATE),--date $(DATE),)

backfill:
	@test -n "$(START)" || (echo "用法: make backfill START=2015-01-01 END=2024-12-31" && exit 2)
	$(PYTHON) scripts/backfill_history.py --profile $(PROFILE) \
		--start $(START) --end $(END) --chunk-days $(CHUNK_DAYS) \
		$(if $(BENCHMARK),--benchmark $(BENCHMARK),--all)

build:
	$(UV) build

clean:
	$(PYTHON) -c "import pathlib, shutil; roots=[pathlib.Path(p) for p in ('src','scripts','tests') if pathlib.Path(p).exists()]; [shutil.rmtree(p, ignore_errors=True) for r in roots for p in r.rglob('__pycache__')]"
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True) for p in map(pathlib.Path, ('.pytest_cache','.ruff_cache','.mypy_cache','build','dist','htmlcov','.coverage'))]"
