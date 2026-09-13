// decision-service: synchronous fraud decisions for RTACE.
//
//	gRPC  :9095  rtace.v1.DecisionService/Decide
//	HTTP  :8090  POST /v1/decide, GET /healthz, GET /metrics
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/reflection"

	rtacev1 "github.com/Duck-luv-pie/RTACE/decision-service/gen/rtace/v1"
	"github.com/Duck-luv-pie/RTACE/decision-service/internal/decision"
)

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, nil)))

	redisAddr := env("REDIS_ADDR", env("REDIS_HOST", "localhost")+":"+env("REDIS_PORT", "6379"))
	grpcAddr := env("GRPC_ADDR", ":9095")
	httpAddr := env("HTTP_ADDR", ":8090")

	rdb := redis.NewClient(&redis.Options{Addr: redisAddr, PoolSize: 64, MinIdleConns: 8})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	if err := rdb.Ping(ctx).Err(); err != nil {
		slog.Error("redis unreachable", "addr", redisAddr, "err", err)
		os.Exit(1)
	}
	cancel()

	luaDir, err := decision.FindLuaDir()
	if err != nil {
		slog.Error("lua scripts not found", "err", err)
		os.Exit(1)
	}
	engine, err := decision.NewEngine(rdb, decision.ConfigFromEnv(), luaDir)
	if err != nil {
		slog.Error("engine init failed", "err", err)
		os.Exit(1)
	}
	srv := &decision.Server{Engine: engine, Ping: func(c context.Context) error { return rdb.Ping(c).Err() }}

	grpcServer := grpc.NewServer()
	rtacev1.RegisterDecisionServiceServer(grpcServer, srv)
	reflection.Register(grpcServer)
	lis, err := net.Listen("tcp", grpcAddr)
	if err != nil {
		slog.Error("grpc listen failed", "addr", grpcAddr, "err", err)
		os.Exit(1)
	}

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.Handle("/", srv.HTTPHandler())
	httpServer := &http.Server{Addr: httpAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}

	go func() {
		slog.Info("gRPC listening", "addr", grpcAddr, "redis", redisAddr, "lua_dir", luaDir)
		if err := grpcServer.Serve(lis); err != nil {
			slog.Error("grpc server stopped", "err", err)
		}
	}()
	go func() {
		slog.Info("HTTP listening", "addr", httpAddr)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("http server stopped", "err", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	slog.Info("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(shutdownCtx)
	grpcServer.GracefulStop()
	_ = rdb.Close()
}
