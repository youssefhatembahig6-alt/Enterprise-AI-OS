# syntax=docker/dockerfile:1
# One backend image runs the API, Celery worker, Celery beat (scheduler) and the
# one-shot migrate/seed commands. Dependencies install from the hashed lockfile.
FROM python:3.13.7-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY fixtures ./fixtures
RUN mkdir -p /app/var && chown app:app /app/var
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

# Test image: adds dev tools and the test suite; never used for the running stack.
FROM runtime AS test
USER root
COPY requirements-dev.lock ./
RUN pip install --require-hashes --no-deps -r requirements-dev.lock
COPY tests ./tests
USER app
CMD ["pytest", "-q"]
