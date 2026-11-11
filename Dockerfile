# syntax=docker/dockerfile:1
# Pythagoras backend image (RELEASE-09). Build for Cloudflare Containers:
#   docker build --platform linux/amd64 -t pythagoras-backend:local .
# One image, fixed roles (see docker-entrypoint.sh): web | job-probe | migrate.
# python:3.12-slim pinned by digest (Debian 13 "trixie", CPython 3.12.16, amd64 manifest list).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHON_DOTENV_DISABLED=1 \
    PORT=8000

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
# Optional build-time-only CA bundle for TLS-intercepting networks:
#   docker build --secret id=build_ca,src=/path/ca-bundle.crt ...
# Secret mounts never enter a layer; without the secret this is a no-op.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
 && pip install -r requirements.txt && pip check

COPY manage.py docker-entrypoint.sh ./
COPY pythagoras ./pythagoras
COPY accounts ./accounts
COPY dawah ./dawah
COPY quiz ./quiz

# Static files are collected at BUILD time into a root-owned (read-only for the
# runtime user) directory so WhiteNoise's manifest exists before the first
# request. Django's settings refuse to import without SECRET_KEY, so the build
# step uses a throwaway random key generated inside this single RUN command. It
# is never written to a layer, ENV, ARG or file, differs per build, and the
# runtime image never contains it: the real SECRET_KEY must be injected at start.
RUN chmod 0555 docker-entrypoint.sh \
 && SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    DEBUG=false STATIC_ROOT=/app/staticfiles \
    python manage.py collectstatic --noinput \
 && chmod -R a-w /app/staticfiles

ENV STATIC_ROOT=/app/staticfiles

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
