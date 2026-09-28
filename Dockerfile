FROM python:3.14-slim
WORKDIR /app
RUN pip install --no-cache-dir --no-compile 'huckleberry-api==0.4.7' 'aionanit==1.12.2'
COPY sync.py /app/sync.py
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=2 CMD python sync.py healthcheck
CMD ["python", "sync.py", "serve"]
