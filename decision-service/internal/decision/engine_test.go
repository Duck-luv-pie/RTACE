package decision

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

var (
	t0     = time.Date(2025, 3, 8, 12, 0, 0, 0, time.UTC)
	sf     = [2]float64{37.77, -122.42}
	london = [2]float64{51.51, -0.13}
)

func newEngine(t *testing.T, mutate func(*Config)) (*Engine, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	cfg := Config{ReplayTTL: 24 * time.Hour, BurstWindow: 60 * time.Second, BurstThreshold: 20, BurstKeyTTL: 120 * time.Second,
		SessionTTL: 7 * 24 * time.Hour, MaxVelocityKmh: 900, ResultTTL: time.Hour, DenyScore: 80, ChallengeScore: 40}
	if mutate != nil {
		mutate(&cfg)
	}
	eng, err := NewEngine(rdb, cfg, "../../../lua")
	if err != nil {
		t.Fatal(err)
	}
	return eng, mr
}

func tx(id string, at time.Time, coords [2]float64) Transaction {
	return Transaction{EventID: id, UserID: "user_1", Amount: 42.5, Merchant: "Amazon", Timestamp: at,
		Location: "US-CA", Latitude: coords[0], Longitude: coords[1]}
}

// The Python side computed these with TransactionEvent.replay_hash(); see tests/test_models.py.
func TestReplayHashMatchesPython(t *testing.T) {
	got := ReplayHash(tx("evt-1", t0, [2]float64{37, -122}))
	if got != "6257e391cc9101ebf997092fd00d8087250f89b1c95c555b9222d0ececdaf8c6" {
		t.Fatalf("hash without microseconds differs from Python: %s", got)
	}
	got = ReplayHash(tx("evt-2", t0.Add(123456*time.Microsecond), [2]float64{37, -122}))
	if got != "4a58c812f6b3cfe021a15e18cf220dac42b2d69a7e57bdf06621ac724480bcc1" {
		t.Fatalf("hash with microseconds differs from Python: %s", got)
	}
}

func TestCleanTransactionIsAllowedAndStored(t *testing.T) {
	eng, mr := newEngine(t, nil)
	d, err := eng.Decide(context.Background(), tx("e1", t0, sf))
	if err != nil {
		t.Fatal(err)
	}
	if d.Verdict != Allow || d.Score != 0 || len(d.Reasons) != 0 {
		t.Fatalf("unexpected decision %+v", d)
	}
	if !mr.Exists("decision:result:e1") || !mr.Exists("session:user_1") || !mr.Exists("burst:user_1:1m") {
		t.Fatal("state not written")
	}
	if v, _ := mr.Get("replay:seen:" + ReplayHash(tx("e1", t0, sf))); v != "decision:e1" {
		t.Fatalf("replay key value %q", v)
	}
}

func TestReplayIsDenied(t *testing.T) {
	eng, _ := newEngine(t, nil)
	ctx := context.Background()
	_, _ = eng.Decide(ctx, tx("e1", t0, sf))
	// Same payload, different event id = a different delivery of the same request
	d, _ := eng.Decide(ctx, tx("e2", t0, sf))
	if d.Verdict != Deny || d.Reasons[0].Type != "replay_attack" || d.Reasons[0].Details["first_seen_record"] != "decision:e1" {
		t.Fatalf("unexpected %+v", d)
	}
}

func TestRetryReturnsCachedVerdict(t *testing.T) {
	eng, _ := newEngine(t, nil)
	ctx := context.Background()
	first, _ := eng.Decide(ctx, tx("e1", t0, sf))
	again, _ := eng.Decide(ctx, tx("e1", t0, sf))
	if !again.Cached || again.DecisionID != first.DecisionID || again.Verdict != first.Verdict {
		t.Fatalf("retry not served from cache: %+v vs %+v", first, again)
	}
}

func TestBurstIsDenied(t *testing.T) {
	eng, _ := newEngine(t, func(c *Config) { c.BurstThreshold = 2 })
	ctx := context.Background()
	var d *Decision
	for i := 0; i < 3; i++ {
		d, _ = eng.Decide(ctx, tx("e"+string(rune('a'+i)), t0.Add(time.Duration(i)*time.Second), sf))
	}
	if d.Verdict != Deny || d.Reasons[0].Type != "fraud_burst" || d.Reasons[0].Details["count_in_window"] != float64(3) {
		t.Fatalf("unexpected %+v", d)
	}
}

func TestImpossibleTravelChallengesAndKeepsBaseline(t *testing.T) {
	eng, mr := newEngine(t, nil)
	ctx := context.Background()
	_, _ = eng.Decide(ctx, tx("e1", t0, sf))
	d, _ := eng.Decide(ctx, tx("e2", t0.Add(time.Hour), london))
	if d.Verdict != Challenge || d.Reasons[0].Type != "geo_velocity_anomaly" {
		t.Fatalf("unexpected %+v", d)
	}
	if d.Reasons[0].Details["velocity_kmh"].(float64) < 900 {
		t.Fatalf("velocity %v", d.Reasons[0].Details["velocity_kmh"])
	}
	if lat := mr.HGet("session:user_1", "lat"); lat != "37.77" {
		t.Fatalf("baseline moved to %s", lat)
	}
	// Back home: consistent with the kept baseline
	d3, _ := eng.Decide(ctx, tx("e3", t0.Add(70*time.Minute), sf))
	if d3.Verdict != Allow {
		t.Fatalf("home transaction after anomaly should be allowed: %+v", d3)
	}
}

func TestQuarantinedUserIsDeniedWithoutTouchingState(t *testing.T) {
	eng, mr := newEngine(t, nil)
	mr.Set("enforce:quarantine:user:user_1", "det-x")
	d, _ := eng.Decide(context.Background(), tx("e1", t0, sf))
	if d.Verdict != Deny || d.Reasons[0].Type != "user_quarantined" {
		t.Fatalf("unexpected %+v", d)
	}
	if mr.Exists("session:user_1") || mr.Exists("decision:result:e1") {
		t.Fatal("state must not be written for refused events")
	}
}

func TestBlockedIPIsDenied(t *testing.T) {
	eng, mr := newEngine(t, nil)
	mr.Set("block:ip:2001-db8--1", "det-x")
	x := tx("e1", t0, sf)
	x.IPAddress = "2001:db8::1"
	d, _ := eng.Decide(context.Background(), x)
	if d.Verdict != Deny || d.Reasons[0].Type != "ip_blocked" {
		t.Fatalf("unexpected %+v", d)
	}
}

func TestPendingStepUpChallengesAndIsRescoredOnceCleared(t *testing.T) {
	eng, mr := newEngine(t, nil)
	ctx := context.Background()
	mr.Set("enforce:step_up:user:user_1", "det-x")
	d, _ := eng.Decide(ctx, tx("e1", t0, sf))
	if d.Verdict != Challenge || d.Reasons[0].Type != "step_up_pending" || mr.Exists("decision:result:e1") {
		t.Fatalf("unexpected %+v", d)
	}
	mr.Del("enforce:step_up:user:user_1") // user completed step-up
	d2, _ := eng.Decide(ctx, tx("e1", t0, sf))
	if d2.Verdict != Allow || d2.Cached {
		t.Fatalf("retry after step-up should be scored fresh: %+v", d2)
	}
}

func TestStoredDecisionHasTheShapeThePythonEngineReads(t *testing.T) {
	eng, mr := newEngine(t, nil)
	ctx := context.Background()
	_, _ = eng.Decide(ctx, tx("e1", t0, sf))
	_, _ = eng.Decide(ctx, tx("e2", t0, sf))
	raw, _ := mr.Get("decision:result:e2")
	var doc map[string]any
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		t.Fatal(err)
	}
	reasons := doc["reasons"].([]any)
	r := reasons[0].(map[string]any)
	if doc["verdict"] != "DENY" || r["type"] != "replay_attack" || r["details"].(map[string]any)["first_seen_record"] != "decision:e1" {
		t.Fatalf("unexpected stored decision: %s", raw)
	}
}

func TestVerdictCombinesSignals(t *testing.T) {
	eng, _ := newEngine(t, nil)
	score, v := eng.verdict([]Reason{{Score: 70}})
	if score != 70 || v != Challenge {
		t.Fatalf("%d %s", score, v)
	}
	score, v = eng.verdict([]Reason{{Score: 70}, {Score: 30}})
	if score != 80 || v != Deny {
		t.Fatalf("%d %s", score, v)
	}
	if s, _ := eng.verdict([]Reason{{Score: 100}, {Score: 100}}); s != 100 {
		t.Fatalf("cap %d", s)
	}
}
