FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
COPY app ./app
COPY ops ./ops
COPY eval ./eval
COPY models ./models
COPY scripts ./scripts
COPY web ./web

RUN uv pip install --system --no-cache .

EXPOSE 8000 8080 8501

RUN chmod +x scripts/demarrer.sh

CMD ["sh", "scripts/demarrer.sh"]
