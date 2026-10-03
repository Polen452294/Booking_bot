# PostgreSQL 17 and its official initialization/volume layout are preserved.
FROM postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
ARG APP_VERSION
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Booking PostgreSQL 17" \
    org.opencontainers.image.version=$APP_VERSION \
    org.opencontainers.image.revision=$VCS_REF \
    org.opencontainers.image.source="https://github.com/Polen452294/Booking_bot"
# The pinned entrypoint invokes only `gosu postgres ENTRYPOINT ARGS...`.
# Alpine's small C implementation drops groups/UID and execs without Go's runtime.
RUN apk add --no-cache su-exec=0.3-r0 && rm /usr/local/bin/gosu
COPY gosu /usr/local/bin/gosu
RUN chmod 0755 /usr/local/bin/gosu \
    && test "$(gosu postgres id -u)" = "$(id -u postgres)"
