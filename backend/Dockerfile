FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Ora2Pg (Perl) for the deterministic first conversion pass. We use its file-input
# mode, so no Oracle client libraries are needed. If the apt package is unavailable
# on your base image, build it from the Ora2Pg GitHub release instead:
#   perl Makefile.PL && make && make install
RUN apt-get update \
 && apt-get install -y --no-install-recommends ora2pg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
