// Package decision scores a transaction synchronously against RTACE's Redis
// state, using the same Lua check as the asynchronous detection engine.
package decision

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// Verdicts.
const (
	Allow     = "ALLOW"
	Challenge = "CHALLENGE"
	Deny      = "DENY"
)

// Reason scores. Replay and burst are hard signals and deny on their own;
// geo velocity is a soft signal and only challenges, matching the
// asynchronous containment policy (quarantine vs step-up).
const (
	scoreReplay     = 100
	scoreBurst      = 80
	scoreGeo        = 70
	scoreQuarantine = 100
	scoreIPBlocked  = 100
)

// Redis key layout, identical to common/enforcement.py and the detectors.
const (
	quarantineKeyPrefix = "enforce:quarantine:user:"
	stepUpKeyPrefix     = "enforce:step_up:user:"
	ipBlockKeyPrefix    = "block:ip:"
	replayKeyPrefix     = "replay:seen:"
	burstKeyPrefix      = "burst:"
	burstKeySuffix      = ":1m"
	sessionKeyPrefix    = "session:"
	resultKeyPrefix     = "decision:result:"
	deliveryPrefix      = "decision:"
	minTimeSeconds      = 1.0
)

// Config mirrors the Python RedisConfig knobs the check depends on.
type Config struct {
	ReplayTTL      time.Duration
	BurstWindow    time.Duration
	BurstThreshold int
	BurstKeyTTL    time.Duration
	SessionTTL     time.Duration
	MaxVelocityKmh float64
	ResultTTL      time.Duration
	DenyScore      int
	ChallengeScore int
}

func envInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(name string, def float64) float64 {
	if v := os.Getenv(name); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}

// ConfigFromEnv reads the same environment variables as the Python services.
func ConfigFromEnv() Config {
	return Config{
		ReplayTTL:      time.Duration(envInt("REDIS_REPLAY_TTL_HOURS", 24)) * time.Hour,
		BurstWindow:    time.Duration(envInt("REDIS_BURST_WINDOW_SECONDS", 60)) * time.Second,
		BurstThreshold: envInt("REDIS_BURST_THRESHOLD", 20),
		BurstKeyTTL:    time.Duration(envInt("REDIS_BURST_KEY_TTL_SECONDS", 120)) * time.Second,
		SessionTTL:     time.Duration(envInt("REDIS_SESSION_TTL_DAYS", 7)) * 24 * time.Hour,
		MaxVelocityKmh: envFloat("GEO_MAX_VELOCITY_KMH", 900),
		ResultTTL:      time.Duration(envInt("DECISION_RESULT_TTL_SECONDS", 86400)) * time.Second,
		DenyScore:      envInt("DECISION_DENY_SCORE", 80),
		ChallengeScore: envInt("DECISION_CHALLENGE_SCORE", 40),
	}
}

// Transaction is the request as the engine sees it.
type Transaction struct {
	EventID   string
	UserID    string
	Amount    float64
	Merchant  string
	Timestamp time.Time
	Location  string
	Latitude  float64
	Longitude float64
	IPAddress string
}

// Reason is one contributing signal.
type Reason struct {
	Type    string         `json:"type"`
	Score   int            `json:"score"`
	Details map[string]any `json:"details"`
}

// Decision is the verdict; it is also what gets stored in Redis for the
// asynchronous engine to re-emit as detections.
type Decision struct {
	DecisionID string    `json:"decision_id"`
	Verdict    string    `json:"verdict"`
	Score      int       `json:"score"`
	Reasons    []Reason  `json:"reasons"`
	Cached     bool      `json:"cached"`
	DecidedAt  time.Time `json:"decided_at"`
	Source     string    `json:"source"`
}

// Engine holds the Redis client and the loaded script.
type Engine struct {
	rdb     redis.Cmdable
	cfg     Config
	txCheck *redis.Script
}

// FindLuaDir returns the first existing directory containing tx_check.lua.
func FindLuaDir(candidates ...string) (string, error) {
	if env := os.Getenv("LUA_DIR"); env != "" {
		candidates = append([]string{env}, candidates...)
	}
	candidates = append(candidates, "lua", "../lua", "/app/lua")
	for _, c := range candidates {
		if c == "" {
			continue
		}
		if _, err := os.Stat(filepath.Join(c, "tx_check.lua")); err == nil {
			return c, nil
		}
	}
	return "", errors.New("tx_check.lua not found; set LUA_DIR")
}

// NewEngine loads lua/tx_check.lua from luaDir.
func NewEngine(rdb redis.Cmdable, cfg Config, luaDir string) (*Engine, error) {
	src, err := os.ReadFile(filepath.Join(luaDir, "tx_check.lua"))
	if err != nil {
		return nil, fmt.Errorf("load tx_check.lua: %w", err)
	}
	return &Engine{rdb: rdb, cfg: cfg, txCheck: redis.NewScript(string(src))}, nil
}

// ReplayHash must produce exactly what TransactionEvent.replay_hash() does in
// Python: sha256("{user_id}|{amount:.2f}|{merchant}|{timestamp.isoformat()}|{location}").
func ReplayHash(tx Transaction) string {
	payload := fmt.Sprintf("%s|%.2f|%s|%s|%s", tx.UserID, tx.Amount, tx.Merchant, pythonISOFormat(tx.Timestamp), tx.Location)
	sum := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(sum[:])
}

// pythonISOFormat mimics datetime.isoformat(): microseconds only when non-zero,
// offset as +HH:MM (UTC prints +00:00, never Z).
func pythonISOFormat(t time.Time) string {
	base := t.Format("2006-01-02T15:04:05")
	if us := t.Nanosecond() / 1000; us != 0 {
		base += fmt.Sprintf(".%06d", us)
	}
	return base + t.Format("-07:00")
}

func sanitizeIP(ip string) string { return strings.ReplaceAll(ip, ":", "-") }

// Decide scores one transaction. It never blocks on anything but Redis.
func (e *Engine) Decide(ctx context.Context, tx Transaction) (*Decision, error) {
	if tx.EventID == "" || tx.UserID == "" {
		return nil, errors.New("event_id and user_id are required")
	}
	if tx.Timestamp.IsZero() {
		tx.Timestamp = time.Now().UTC()
	}
	now := time.Now().UTC()
	d := &Decision{DecisionID: "dec-" + uuid.NewString(), DecidedAt: now, Source: "decision-service", Reasons: []Reason{}}

	// 1. Enforcement state: one pipelined round trip. ------------------------
	pipe := e.rdb.Pipeline()
	qCmd := pipe.Exists(ctx, quarantineKeyPrefix+tx.UserID)
	sCmd := pipe.Exists(ctx, stepUpKeyPrefix+tx.UserID)
	var ipCmd *redis.IntCmd
	if tx.IPAddress != "" {
		ipCmd = pipe.Exists(ctx, ipBlockKeyPrefix+sanitizeIP(tx.IPAddress))
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return nil, fmt.Errorf("enforcement lookup: %w", err)
	}
	if qCmd.Val() > 0 {
		d.Reasons = append(d.Reasons, Reason{Type: "user_quarantined", Score: scoreQuarantine, Details: map[string]any{}})
		d.Score, d.Verdict = scoreQuarantine, Deny
		return d, nil // refused outright; no state advanced, nothing stored
	}
	if ipCmd != nil && ipCmd.Val() > 0 {
		d.Reasons = append(d.Reasons, Reason{Type: "ip_blocked", Score: scoreIPBlocked, Details: map[string]any{"ip_address": tx.IPAddress}})
		d.Score, d.Verdict = scoreIPBlocked, Deny
		return d, nil
	}
	if sCmd.Val() > 0 {
		// The user owes a step-up. Do not advance state or store a verdict:
		// once the step-up is cleared, the retry with the same event_id must
		// be scored normally.
		d.Reasons = append(d.Reasons, Reason{Type: "step_up_pending", Score: scoreGeo, Details: map[string]any{}})
		d.Score, d.Verdict = scoreGeo, Challenge
		return d, nil
	}

	// 2. The shared Lua check. --------------------------------------------
	nowTS := float64(tx.Timestamp.UnixNano()) / 1e9
	keys := []string{
		replayKeyPrefix + ReplayHash(tx),
		burstKeyPrefix + tx.UserID + burstKeySuffix,
		sessionKeyPrefix + tx.UserID,
	}
	args := []any{
		deliveryPrefix + tx.EventID,
		int(e.cfg.ReplayTTL.Seconds()),
		tx.EventID,
		strconv.FormatFloat(nowTS, 'f', -1, 64),
		strconv.FormatFloat(nowTS-e.cfg.BurstWindow.Seconds(), 'f', -1, 64),
		int(e.cfg.BurstKeyTTL.Seconds()),
		strconv.FormatFloat(tx.Latitude, 'f', -1, 64),
		strconv.FormatFloat(tx.Longitude, 'f', -1, 64),
		int(e.cfg.SessionTTL.Seconds()),
		strconv.FormatFloat(e.cfg.MaxVelocityKmh, 'f', -1, 64),
		strconv.FormatFloat(minTimeSeconds, 'f', -1, 64),
	}
	raw, err := e.txCheck.Run(ctx, e.rdb, keys, args...).Slice()
	if err != nil {
		return nil, fmt.Errorf("tx_check.lua: %w", err)
	}
	if len(raw) < 9 {
		return nil, fmt.Errorf("tx_check.lua returned %d fields", len(raw))
	}
	replayStatus, _ := raw[0].(string)
	storedRef, _ := raw[1].(string)
	burstCount, _ := raw[2].(int64)
	geoStatus, _ := raw[3].(string)

	// A retry of an event we already decided returns the stored verdict.
	if replayStatus == "redelivery" {
		if cached, err := e.rdb.Get(ctx, resultKeyPrefix+tx.EventID).Result(); err == nil {
			var prev Decision
			if json.Unmarshal([]byte(cached), &prev) == nil {
				prev.Cached = true
				return &prev, nil
			}
		}
		// No stored verdict (expired or crashed before storing): fall through
		// and score again; the script's state updates were idempotent.
	}

	// 3. Interpret. -----------------------------------------------------------
	if replayStatus == "replay" {
		d.Reasons = append(d.Reasons, Reason{Type: "replay_attack", Score: scoreReplay,
			Details: map[string]any{"first_seen_record": storedRef}})
	}
	if int(burstCount) > e.cfg.BurstThreshold {
		d.Reasons = append(d.Reasons, Reason{Type: "fraud_burst", Score: scoreBurst, Details: map[string]any{
			"count_in_window": float64(burstCount), "window_seconds": e.cfg.BurstWindow.Seconds(), "threshold": float64(e.cfg.BurstThreshold)}})
	}
	if geoStatus == "anomaly" {
		dist, _ := strconv.ParseFloat(str(raw[4]), 64)
		elapsed, _ := strconv.ParseFloat(str(raw[5]), 64)
		vel, _ := strconv.ParseFloat(str(raw[6]), 64)
		plat, _ := strconv.ParseFloat(str(raw[7]), 64)
		plon, _ := strconv.ParseFloat(str(raw[8]), 64)
		d.Reasons = append(d.Reasons, Reason{Type: "geo_velocity_anomaly", Score: scoreGeo, Details: map[string]any{
			"distance_km": dist, "elapsed_seconds": elapsed, "velocity_kmh": vel, "threshold_kmh": e.cfg.MaxVelocityKmh,
			"from_location": []any{plat, plon}, "to_location": []any{tx.Latitude, tx.Longitude}}})
	}
	d.Score, d.Verdict = e.verdict(d.Reasons)

	// 4. Persist for retries and for the asynchronous engine. ---------------
	if body, err := json.Marshal(d); err == nil {
		if err := e.rdb.Set(ctx, resultKeyPrefix+tx.EventID, body, e.cfg.ResultTTL).Err(); err != nil {
			return nil, fmt.Errorf("store decision: %w", err)
		}
	}
	return d, nil
}

// verdict combines reasons: the strongest signal plus 10 per additional signal, capped at 100.
func (e *Engine) verdict(reasons []Reason) (int, string) {
	if len(reasons) == 0 {
		return 0, Allow
	}
	maxScore := 0
	for _, r := range reasons {
		if r.Score > maxScore {
			maxScore = r.Score
		}
	}
	score := min(100, maxScore+10*(len(reasons)-1))
	switch {
	case score >= e.cfg.DenyScore:
		return score, Deny
	case score >= e.cfg.ChallengeScore:
		return score, Challenge
	default:
		return score, Allow
	}
}

func str(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case int64:
		return strconv.FormatInt(t, 10)
	default:
		return ""
	}
}
