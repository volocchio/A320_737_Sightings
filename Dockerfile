FROM python:3.12-slim

WORKDIR /app

# git for deploy webhook pulls; docker CLI for rebuilding the container
RUN apt-get update \
    && apt-get install -y git curl \
    && curl -fsSL https://get.docker.com | sh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
