FROM golang:1.22 AS builder

ARG VERSION=dev

WORKDIR /src
COPY go.mod go.sum ./
COPY cmd ./cmd
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w -X main.agentVersion=${VERSION}" -o /out/cluster-node-agent ./cmd/node-agent \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/cluster-agent-updater ./cmd/agent-updater

FROM scratch AS artifact
COPY --from=builder /out/cluster-node-agent /cluster-node-agent
COPY --from=builder /out/cluster-agent-updater /cluster-agent-updater
CMD ["/cluster-node-agent"]
