package decision

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPDecideAcceptsTheSimulatorsJSON(t *testing.T) {
	eng, _ := newEngine(t, nil)
	srv := &Server{Engine: eng}
	h := srv.HTTPHandler()
	body := `{"transaction":{"event_id":"e1","user_id":"user_1","amount":42.5,"merchant":"Amazon",
	  "timestamp":"2025-03-08T12:00:00.123456+00:00","location":"US-CA","latitude":37.0,"longitude":-122.0}}`
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/decide", strings.NewReader(body)))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"verdict":"ALLOW"`) {
		t.Fatalf("%d %s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder() // same payload, new event id -> replay
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/decide", strings.NewReader(strings.Replace(body, `"e1"`, `"e2"`, 1))))
	if !strings.Contains(rec.Body.String(), `"verdict":"DENY"`) || !strings.Contains(rec.Body.String(), `replay_attack`) {
		t.Fatalf("%s", rec.Body.String())
	}
}

func TestHTTPRejectsBadInput(t *testing.T) {
	eng, _ := newEngine(t, nil)
	h := (&Server{Engine: eng}).HTTPHandler()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/decide", strings.NewReader(`{"transaction":{"amount":1}}`)))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d %s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/decide", strings.NewReader(`not json`)))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}
