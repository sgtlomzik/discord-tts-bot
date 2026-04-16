FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg libffi-dev libnacl-dev libopus0 build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "-u", "bot.py"]
