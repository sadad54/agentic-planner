.PHONY: install test demo clean

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest

demo:
	python -m scripts.demo

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info
