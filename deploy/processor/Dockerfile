FROM mirror.gcr.io/library/python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOFILL_PROCESS_QUEUE=/work

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libreoffice-calc \
        libreoffice-writer \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 autofill \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin autofill

WORKDIR /app
COPY --from=server src/autofill/__init__.py /app/autofill/__init__.py
COPY --from=server src/autofill/adapters/__init__.py /app/autofill/adapters/__init__.py
COPY --from=server src/autofill/adapters/processor/ /app/autofill/adapters/processor/

USER 10001:10001
CMD ["python", "-m", "autofill.adapters.processor.processor"]
