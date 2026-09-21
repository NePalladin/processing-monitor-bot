FROM python:3.11-slim

# Устанавливаем часовые пояса и сразу чистим мусор apt, чтобы образ остался легким
RUN apt-get update && apt-get install -y tzdata && \
    ln -fs /usr/share/zoneinfo/Europe/Kyiv /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Kyiv

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot_imap_oop.py"]
