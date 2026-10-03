# Official upstream socket proxy, pinned to the audited v0.5.0 release image.
# Only the vulnerable OpenSSL packages are replaced; upstream ACL logic is preserved.
FROM ghcr.io/tecnativa/docker-socket-proxy@sha256:1f5038b54f06c3e18422902cf00ba21803d1c97805aae032e5e6673d532d3459
ARG APP_VERSION
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Booking restricted Docker API" \
    org.opencontainers.image.version=$APP_VERSION \
    org.opencontainers.image.revision=$VCS_REF \
    org.opencontainers.image.source="https://github.com/Polen452294/Booking_bot"
RUN apk add --no-cache libcrypto3=3.5.9-r0 libssl3=3.5.9-r0 pcre2=10.49-r0
