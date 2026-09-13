.PHONY: proto build test run

# Regenerate gen/ from proto/ (needs protoc, protoc-gen-go, protoc-gen-go-grpc).
proto:
	protoc -I proto \
	  --go_out=gen --go_opt=paths=source_relative \
	  --go-grpc_out=gen --go-grpc_opt=paths=source_relative \
	  proto/rtace/v1/decision.proto

build:
	CGO_ENABLED=0 go build -o bin/decision-service ./cmd/decision-service

test:
	go test ./...

run:
	go run ./cmd/decision-service
