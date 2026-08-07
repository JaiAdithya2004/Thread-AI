FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2 and fastembed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 7070
CMD ["python", "-m", "brain.wrappers.api"]
