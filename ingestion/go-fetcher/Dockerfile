# SENTINEL go-fetcher — multi-stage build, distroless-style runtime.

FROM golang:1.24-alpine AS build
WORKDIR /src

# Cache dependencies separately from source so a code change does not refetch modules.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/go-fetcher .

FROM alpine:3.21
RUN apk add --no-cache ca-certificates tzdata && \
    adduser -D -u 10001 sentinel

COPY --from=build /out/go-fetcher /usr/local/bin/go-fetcher

USER sentinel
EXPOSE 8088

ENV PORT=8088 \
    FINGERPRINT_PROFILE=chrome \
    FINGERPRINT_PLATFORM=windows \
    RATE_PER_SECOND=5 \
    RATE_BURST=10 \
    MAX_CONCURRENCY=16 \
    REQUEST_TIMEOUT_MS=20000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O- http://127.0.0.1:8088/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/go-fetcher"]
