FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Lower bounds only for now. After the first successful build, pin exact versions:
# docker compose run --rm api pip freeze > requirements.lock
