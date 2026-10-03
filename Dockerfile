FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-compile -r /app/requirements.txt
COPY sync.py config.py errors.py intervals.py storage.py clients.py ownership.py service.py huckleberry_sleep.py /app/
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 CMD python sync.py healthcheck
CMD ["python", "sync.py", "serve"]
