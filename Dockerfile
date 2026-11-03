# syntax=docker/dockerfile:1
# devdocs CLI image. The build stage makes the wheel from pyproject.toml,
# README.md, LICENSE and devdocs/; the runtime stage installs it on a clean
# python:3.13-slim and runs it as uid 10001 in /work, the only path it writes.
# Run it as yourself so docs/ and .devdocs/ belong to you:
#   docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/work" \
#     -e GEMINI_API_KEY ghcr.io/bgantavya/devdocs generate
# `docker stop` sends SIGINT, devdocs' Ctrl-C: nothing new is sent, in-flight
# calls finish and are cached, the pages are written, and it exits 130. Allow
# 150 s for that (`docker stop -t 150`, `docker run --stop-timeout 150`); by
# default SIGKILL follows after 10 s. A second signal a second or more later
# (`docker kill -s INT`) stops at once; the answers received stay cached.

FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY devdocs/ devdocs/
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist .

FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 devdocs \
 && useradd --uid 10001 --gid 10001 --no-log-init --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin devdocs \
 && install -d -o 10001 -g 10001 /work
# Bind mount: the wheel never lands in a layer. No extras and --no-index, so
# nothing is fetched and devdocs runs on the standard library alone. That leaves
# provider = "anthropic-sdk" refused here, with a hint; provider = "anthropic"
# reaches the same API over the built-in HTTP client.
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    pip install --no-cache-dir --no-index --root-user-action=ignore /dist/*.whl
WORKDIR /work
USER 10001:10001

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="devdocs" \
      org.opencontainers.image.description="Documentation from source code: exact facts from parsers, model prose only where it adds something, on a hard budget." \
      org.opencontainers.image.source="https://github.com/bgantavya/devDocs" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# devdocs stops alike on SIGTERM (exit 143), but the kernel drops one sent to
# PID 1 before its handler is set; Python handles SIGINT from startup.
STOPSIGNAL SIGINT
ENTRYPOINT ["devdocs"]
CMD ["--help"]
