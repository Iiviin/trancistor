FROM python:3.12-slim-bookworm

# Versions pinned so a routine rebuild can't silently jump a numpy major
# version (the code is numpy-2 clean today; keep it deliberate, not lucky).
RUN apt-get update && apt-get install -y \
    pulseaudio-utils \
    && pip install --no-cache-dir numpy==2.2.6 requests==2.32.3 \
    && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY run.sh /run.sh
COPY beat_detector.py /beat_detector.py
RUN chmod +x /run.sh

CMD ["/run.sh"]