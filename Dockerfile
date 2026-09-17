FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.10
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["power-forecast"]
