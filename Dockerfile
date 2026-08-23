FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY notify.py .

# Pre-create /state owned by the runtime UID. Docker copies this ownership
# onto the named volume at first mount; without it the volume is root-owned
# and a non-root container cannot write to it.
RUN mkdir -p /state && chown 1000:1000 /state

USER 1000:1000

CMD ["python", "-u", "notify.py"]
