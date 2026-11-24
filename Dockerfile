# discord-client-mcp: the read-only discord-client commands as an MCP server,
# meant to run as a sidecar next to an agent container. See docs/mcp-sidecar.md.
#
# Tokens and settings come in at runtime through the environment (env_file in
# compose). Nothing secret is copied into the image: .dockerignore admits only
# the package and the files its build metadata needs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /src
# README.md is named as the package readme in pyproject.toml, so the build needs it.
COPY pyproject.toml README.md LICENSE ./
COPY discord_client ./discord_client
RUN pip install ".[mcp]" && rm -rf /src

RUN useradd --create-home --uid 10001 app
USER app
WORKDIR /home/app

EXPOSE 8000
CMD ["discord-client-mcp", "--host", "0.0.0.0", "--port", "8000"]
