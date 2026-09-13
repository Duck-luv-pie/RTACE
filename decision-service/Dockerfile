# Build from the repository root so the shared Lua scripts are in the context:
#   docker build -f decision-service/Dockerfile -t rtace-decision:local .
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY decision-service/go.mod decision-service/go.sum ./
RUN go mod download
COPY decision-service/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/decision-service ./cmd/decision-service

FROM alpine:3.21
RUN adduser -S -u 10001 rtace
WORKDIR /app
COPY --from=build /out/decision-service /app/decision-service
COPY lua/ /app/lua/
USER rtace
ENV LUA_DIR=/app/lua
EXPOSE 9095 8090
ENTRYPOINT ["/app/decision-service"]
