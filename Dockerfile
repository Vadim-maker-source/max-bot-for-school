FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py bot.py max_api.py database.py lostfound.py gigachat_api.py ./
RUN python max_api.py ca --bundle /opt/max-ca.pem --download-only \
    && mkdir -p /app/private/photos && chown -R 10001:10001 /app/private
USER 10001:10001
EXPOSE 8000
CMD ["python", "main.py"]
