FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM mirror.gcr.io/library/python:3.12.15-slim-bookworm AS build

COPY --from=uv /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

FROM mirror.gcr.io/library/python:3.12.15-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 autofill \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin autofill

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY src/ ./src/
COPY container-entrypoint.sh /usr/local/bin/container-entrypoint
RUN chmod 0555 /usr/local/bin/container-entrypoint

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/container-entrypoint"]
CMD ["python", "-m", "autofill.adapters.http.app"]
