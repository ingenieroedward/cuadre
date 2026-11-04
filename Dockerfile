FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir /data && chown app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
