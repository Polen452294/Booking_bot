# Preserve Redis 7.4, its UID, entrypoint and persistent volume layout.
FROM redis:7.4-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
ARG APP_VERSION
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Booking Redis 7.4" \
    org.opencontainers.image.version=$APP_VERSION \
    org.opencontainers.image.revision=$VCS_REF \
    org.opencontainers.image.source="https://github.com/Polen452294/Booking_bot"
RUN apk add --no-cache libcrypto3=3.3.7-r2 libssl3=3.3.7-r2
COPY booking-entrypoint.sh /usr/local/bin/booking-entrypoint.sh
RUN chmod 0755 /usr/local/bin/booking-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/booking-entrypoint.sh"]
