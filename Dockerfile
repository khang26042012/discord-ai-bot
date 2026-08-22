FROM python:3.11-slim

WORKDIR /app

# ffmpeg (phát nhạc) + nodejs/npm/git (sinh PO token YouTube qua bgutil)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bộ sinh PO token bgutil — chạy chế độ script, giúp link googlevideo không bị 403
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm install --no-audit --no-fund \
    && npm run build

COPY . .

CMD ["python", "bot.py"]
