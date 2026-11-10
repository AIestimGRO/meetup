FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
RUN mkdir -p /data && useradd -u 10001 -m app && chown app:app /data
USER app
CMD ["python", "bot.py"]
