FROM python:3.12-slim

# ffmpeg — для Whisper (декодирование аудио)
# tzdata — база часовых поясов для zoneinfo (Europe/Moscow)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
