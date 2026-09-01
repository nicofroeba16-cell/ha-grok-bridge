ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8

RUN apk add --no-cache \
    python3 \
    git \
    openssh-client \
    curl \
    bash

WORKDIR /opt/ha-grok-bridge

COPY cloud_poll.py /opt/ha-grok-bridge/cloud_poll.py
COPY ha /usr/local/bin/ha
COPY run.sh /run.sh

RUN chmod a+x /run.sh /usr/local/bin/ha \
    && ln -sf /usr/local/bin/ha /usr/bin/ha

CMD ["/run.sh"]
