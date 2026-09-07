FROM ghcr.io/astral-sh/uv:0.9.28-python3.11-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/test-env/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
       bash build-essential curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/test-env
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --python 3.11 --no-install-project \
      --extra all --extra dev --extra anthropic --extra mistral \
      --extra fal --extra modal --extra daytona --extra hindsight \
      --extra parallel-web \
    && sha256sum uv.lock | cut -d' ' -f1 > /opt/test-env/uv.lock.sha256

COPY docker/local-test-runner/runner.py /opt/hermes-test-runner/runner.py
RUN /usr/sbin/useradd --uid 1000 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /spool /work /tmp \
    && chown -R 1000:1000 /spool /work /tmp /opt/hermes-test-runner

USER 1000:1000
WORKDIR /work
ENTRYPOINT ["/opt/test-env/.venv/bin/python", "/opt/hermes-test-runner/runner.py"]
