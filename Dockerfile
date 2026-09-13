# One image for everything: the API (default command), migrations, and the
# pipeline CLI (`docker compose run --rm api python -m ava_eval <command>`).

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, source second. Docker caches each step, so editing code
# only re-runs the COPY below instead of reinstalling every package.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY ava_eval ./ava_eval
COPY scripts ./scripts
COPY sample_data ./sample_data

# Don't run as root: if the app were ever compromised, the attacker would not
# own the container.
RUN useradd --create-home --uid 1000 ava && chown -R ava:ava /app
USER ava

EXPOSE 8000

# 0.0.0.0, not 127.0.0.1: inside a container, localhost is the container itself,
# so binding to it would make the API unreachable from your machine.
CMD ["uvicorn", "ava_eval.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
