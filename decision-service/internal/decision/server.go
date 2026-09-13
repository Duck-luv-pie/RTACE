package decision

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	rtacev1 "github.com/Duck-luv-pie/RTACE/decision-service/gen/rtace/v1"
)

// Server implements rtace.v1.DecisionService over gRPC and the same call over HTTP/JSON.
type Server struct {
	rtacev1.UnimplementedDecisionServiceServer
	Engine *Engine
	Ping   func(context.Context) error
}

// Decide is the gRPC entry point.
func (s *Server) Decide(ctx context.Context, req *rtacev1.DecideRequest) (*rtacev1.DecideResponse, error) {
	if req.GetTransaction() == nil {
		return nil, status.Error(codes.InvalidArgument, "transaction is required")
	}
	resp, err := s.decide(ctx, req.GetTransaction())
	if err != nil {
		if errors.Is(err, errInvalid) {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	return resp, nil
}

var errInvalid = errors.New("invalid request")

func (s *Server) decide(ctx context.Context, t *rtacev1.Transaction) (*rtacev1.DecideResponse, error) {
	start := time.Now()
	tx := Transaction{
		EventID: t.GetEventId(), UserID: t.GetUserId(), Amount: t.GetAmount(), Merchant: t.GetMerchant(),
		Location: t.GetLocation(), Latitude: t.GetLatitude(), Longitude: t.GetLongitude(), IPAddress: t.GetIpAddress(),
	}
	if t.GetTimestamp() != nil {
		tx.Timestamp = t.GetTimestamp().AsTime()
	}
	if tx.EventID == "" || tx.UserID == "" {
		DecisionErrorsTotal.Inc()
		return nil, errors.Join(errInvalid, errors.New("event_id and user_id are required"))
	}
	d, err := s.Engine.Decide(ctx, tx)
	if err != nil {
		DecisionErrorsTotal.Inc()
		slog.Error("decide failed", "event_id", tx.EventID, "err", err)
		return nil, err
	}
	DecisionLatency.Observe(time.Since(start).Seconds())
	DecisionsTotal.WithLabelValues(d.Verdict).Inc()
	for _, r := range d.Reasons {
		DecisionReasonsTotal.WithLabelValues(r.Type).Inc()
	}
	if d.Verdict != Allow {
		slog.Info("decision", "verdict", d.Verdict, "score", d.Score, "user_id", tx.UserID, "event_id", tx.EventID, "reasons", reasonTypes(d.Reasons), "cached", d.Cached)
	}
	return toProto(d)
}

func reasonTypes(rs []Reason) []string {
	out := make([]string, 0, len(rs))
	for _, r := range rs {
		out = append(out, r.Type)
	}
	return out
}

func toProto(d *Decision) (*rtacev1.DecideResponse, error) {
	resp := &rtacev1.DecideResponse{
		DecisionId: d.DecisionID, Score: int32(d.Score), Cached: d.Cached, DecidedAt: timestamppb.New(d.DecidedAt),
		Verdict: map[string]rtacev1.Verdict{Allow: rtacev1.Verdict_ALLOW, Challenge: rtacev1.Verdict_CHALLENGE, Deny: rtacev1.Verdict_DENY}[d.Verdict],
	}
	for _, r := range d.Reasons {
		details, err := structpb.NewStruct(r.Details)
		if err != nil {
			return nil, err
		}
		resp.Reasons = append(resp.Reasons, &rtacev1.Reason{Type: r.Type, Score: int32(r.Score), Details: details})
	}
	return resp, nil
}

// HTTPHandler serves POST /v1/decide (JSON DecideRequest), GET /healthz.
func (s *Server) HTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/decide", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var req rtacev1.DecideRequest
		if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(body, &req); err != nil {
			http.Error(w, "invalid JSON: "+err.Error(), http.StatusBadRequest)
			return
		}
		if req.GetTransaction() == nil {
			http.Error(w, `{"error":"transaction is required"}`, http.StatusBadRequest)
			return
		}
		resp, err := s.decide(r.Context(), req.GetTransaction())
		if err != nil {
			code := http.StatusServiceUnavailable
			if errors.Is(err, errInvalid) {
				code = http.StatusBadRequest
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(code)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		out, _ := protojson.MarshalOptions{UseProtoNames: true}.Marshal(resp)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(out)
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if s.Ping != nil {
			if err := s.Ping(r.Context()); err != nil {
				http.Error(w, `{"status":"redis unavailable"}`, http.StatusServiceUnavailable)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok","redis":"connected"}`))
	})
	return mux
}
