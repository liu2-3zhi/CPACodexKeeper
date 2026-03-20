set shell := ["bash", "-cu"]

install:
    python -m pip install -r requirements.txt

test:
    python -m unittest discover -s tests

run-once:
    python main.py --once

dry-run:
    python main.py --once --dry-run

daemon:
    python main.py

docker-build:
    docker build -t cpacodexkeeper .

docker-up:
    docker compose up -d --build

docker-down:
    docker compose down
