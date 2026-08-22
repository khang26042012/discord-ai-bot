FROM python:3.11-slim

WORKDIR /app

# ffmpeg (phát nhạc) + nodejs/npm/git (sinh PO token YouTube qua bgutil)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs npm ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bộ sinh PO token bgutil - clone về HOME để plugin tự nhận diện (script mode)
# Build: npm ci + npx tsc -> tạo server/build/generate_once.js
RUN git clone --single-branch --depth 1 --branch 1.3.2 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider/server \
    && npm ci --no-audit --no-fund \
    && npx tsc

COPY . .

CMD ["python", "bot.py"]
