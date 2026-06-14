ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.11-alpine3.18
ARG BUILD_VERSION
ARG BUILD_ARCH

FROM ${BUILD_FROM}

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="addon" \
  io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY bridge ./bridge
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
