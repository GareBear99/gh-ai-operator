# syntax=docker/dockerfile:1.7
# Container image for the ARC GitHub AI Operator.
# Runs review_target.py (or scout.py) in a disposable sandbox with
# network access but no access to the host filesystem beyond a
# mounted output directory.
#
# Build:
#   docker build -t arc-ai-operator:latest .
#
# Run (single-target review):
#   docker run --rm \
#       -e GITHUB_TOKEN \
#       -e CLOUDFLARE_ACCOUNT_ID \
#       -e CLOUDFLARE_API_TOKEN \
#       -e TARGET_URL=https://github.com/owner/repo \
#       -e REVIEW_TYPE="Full repo review" \
#       -e REVIEW_DEPTH=standard \
#       -e FOCUS="what to look at" \
#       -v "$PWD/output:/work/output" \
#       arc-ai-operator:latest
#
# The container is intentionally minimal: python + git + gh + curl.
# No build tools, no compilers, no package managers for the target's
# language — the operator does not execute target code.

FROM python:3.12-slim

# Non-root user for defense in depth
RUN groupadd --gid 1000 arc && useradd --uid 1000 --gid arc --shell /bin/bash --create-home arc

# System deps: git (clone targets), gh (optional post-back), curl (health).
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update -y && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get clean

WORKDIR /work

# Install Python deps first for layer caching
COPY requirements.txt /work/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /work/requirements.txt

# Source
COPY github_ai_operator /work/github_ai_operator
COPY review_target.py   /work/review_target.py
COPY scout.py           /work/scout.py
COPY config.example.json /work/config.example.json
COPY pyproject.toml     /work/pyproject.toml

# Output directory is a mount point so results survive container exit.
RUN mkdir -p /work/output && chown -R arc:arc /work

USER arc

# Default: run a single-target review. Override command for scout mode:
#   docker run arc-ai-operator python scout.py --config config.json
CMD ["python", "review_target.py"]
