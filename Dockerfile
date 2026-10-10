FROM python:3.12.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 lab \
    && useradd --uid 10001 --gid lab --no-create-home lab \
    && mkdir -p /var/lib/lab && chown lab:lab /var/lib/lab
COPY --chown=lab:lab lab ./lab
COPY --chown=lab:lab scripts ./scripts
USER 10001:10001
CMD ["python", "-m", "uvicorn", "lab.application:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "5", "--limit-concurrency", "64"]
