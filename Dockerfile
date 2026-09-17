FROM python:3.12-slim
# Patch the OS packages the base layer ships with. ECR scan-on-push reported
# CRITICAL findings in perl and glibc that came entirely from the base image,
# none from project code, and the base tag lags its own security updates.
RUN apt-get update \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.10
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["power-forecast"]
