ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8
WORKDIR /opt/ha-file-sync-bridge

RUN apk add --no-cache \
    python3 \
    py3-yaml \
    git \
    openssh-client

COPY file_bridge.py /opt/ha-file-sync-bridge/file_bridge.py
COPY ai_control.py /opt/ha-file-sync-bridge/ai_control.py
COPY sitecustomize.py /opt/ha-file-sync-bridge/sitecustomize.py
COPY run.sh /run.sh
COPY known_hosts /opt/ha-file-sync-bridge/known_hosts

RUN chmod 0755 /run.sh /opt/ha-file-sync-bridge/file_bridge.py /opt/ha-file-sync-bridge/ai_control.py

ENTRYPOINT ["/run.sh"]
