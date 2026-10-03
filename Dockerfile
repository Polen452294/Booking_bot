FROM python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111 AS runtime

ARG APP_VERSION
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Booking bot" \
    org.opencontainers.image.version=$APP_VERSION \
    org.opencontainers.image.revision=$VCS_REF \
    org.opencontainers.image.source="https://github.com/Polen452294/Booking_bot"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md requirements.lock requirements-build.lock ./
COPY src ./src
COPY alembic.ini ./

RUN python -c "import os; from src.booking_bot.version import __version__; assert __version__ == os.environ['APP_VERSION'], 'APP_VERSION must match version.py'" \
    && python -m pip install --require-hashes -r requirements.lock -r requirements-build.lock \
    && python -m pip install --no-deps --no-build-isolation . \
    && python -m pip check \
    && addgroup -g 10001 booking \
    && adduser -D -H -u 10001 -G booking -s /sbin/nologin booking

# Application code stays root-owned and read-only to the runtime user.
USER 10001:10001

EXPOSE 8000

CMD ["python", "-m", "booking_bot.server"]
