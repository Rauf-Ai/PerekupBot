FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' > /tmp/requirements.txt \
    && pip install --timeout 120 --retries 10 -r /tmp/requirements.txt
COPY . .
RUN mkdir -p /srv/data
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
