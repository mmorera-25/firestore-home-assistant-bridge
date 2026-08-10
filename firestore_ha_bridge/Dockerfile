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
# Core dependency must install; google listen SDKs are best-effort (REST poll is primary).
RUN pip3 install --no-cache-dir --break-system-packages 'requests>=2.31.0' \
  && pip3 install --no-cache-dir --break-system-packages 'google-auth>=2.29.0' 'google-cloud-firestore>=2.16.0' \
  || echo "WARN: google-cloud-firestore unavailable on this arch; bridge will use catch-up poll only."

COPY bridge ./bridge
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
