FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd --system --no-create-home appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser

EXPOSE 8181
CMD ["python", "app.py"]
