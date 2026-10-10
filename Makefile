.PHONY: setup seed run

setup:
	pip install -r requirements.txt

seed:
	python sample_seed.py

run:
	uvicorn app.main:app --port 8000 --reload