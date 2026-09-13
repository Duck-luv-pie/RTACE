package decision

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	DecisionsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "decisions_total",
		Help: "Synchronous decisions by verdict",
	}, []string{"verdict"})
	DecisionReasonsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "decision_reasons_total",
		Help: "Signals that contributed to decisions",
	}, []string{"type"})
	DecisionLatency = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "decision_latency_seconds",
		Help:    "End-to-end Decide latency including Redis",
		Buckets: []float64{0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1},
	})
	DecisionErrorsTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "decision_errors_total",
		Help: "Decide calls that failed (Redis or validation)",
	})
)
