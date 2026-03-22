# Sürekli çalışan worker — Railway, Fly.io, Render, kendi sunucun (Docker).
# Vercel bu Dockerfile ile uyumlu değildir (aşağıdaki gerekçe).
#
# Örnek (Railway / Fly): Ortam değişkenlerini panelden ver:
#   DISCORD_WEBHOOK_URL, TOPIC_URL, COOKIE, POLL_INTERVAL=600, STATE_FILE=/data/state.json
# Kalıcı disk ekle ve /data ya bağla; yoksa yeniden deployda seen_ids sıfırlanır.

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV STATE_FILE=/data/.forum_monitor_state.json
ENV POLL_INTERVAL=600

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forum_discord_webhook.py .

# Ortam: DISCORD_WEBHOOK_URL, TOPIC_URL, COOKIE (veya COOKIES_FILE). POLL_INTERVAL panelden değiştirilebilir.
CMD ["python", "forum_discord_webhook.py"]
