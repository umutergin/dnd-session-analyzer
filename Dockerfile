FROM python:3.13-slim

# ffmpeg: audio merge/compression. libopus0: decoding Discord voice. git: py-cord is
# installed from a pinned commit (see pyproject.toml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "src.bot.main"]
