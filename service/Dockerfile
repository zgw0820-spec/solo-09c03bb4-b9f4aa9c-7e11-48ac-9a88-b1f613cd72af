FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

# iproute2 provides tc + ip; iputils-ping is used by the connectivity check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       iproute2 \
       iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY linklab /app/linklab
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /run/lab

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8080", "--threads=8", "linklab.web:app"]
