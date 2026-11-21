# ratiomaster - announce emulator, qBittorrent API source mode.
# Pure Python stdlib: no pip layer.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODE=qbit \
    LOG_FILE=""

WORKDIR /app
COPY ratiomaster.py config.ini ./

RUN adduser -D -H -u 1000 ratiomaster
USER ratiomaster

# docker stop sends SIGTERM -> every session sends event=stopped before exit.
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/app/ratiomaster.py"]
CMD ["--mode", "qbit"]
