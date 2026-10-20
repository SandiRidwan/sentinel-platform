// Command go-fetcher is the high-volume HTTP path of SENTINEL.
//
// Endpoints
//
//	GET  /healthz      liveness
//	GET  /metrics      Prometheus text format
//	POST /fetch        {"url": "..."}                -> one Result
//	POST /fetch/batch  {"urls": [...], "concurrency": 8} -> Stats + Results
//
// Configuration is environment only, so the same binary runs locally, in a container and
// on Kubernetes without a config file to keep in sync.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/fetcher"
	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/fingerprint"
	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/metrics"
)

var (
	reg          = metrics.New()
	countFetched = reg.Counter("sentinel_go_fetcher_requests_total", "Total fetch attempts served.")
	countFailed  = reg.Counter("sentinel_go_fetcher_failures_total", "Fetch attempts that returned an error.")
	countBytes   = reg.Counter("sentinel_go_fetcher_bytes_total", "Response bytes read.")
)

func main() {
	cfg, err := configFromEnv()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	f, err := fetcher.New(cfg.fetcher)
	if err != nil {
		log.Fatalf("fetcher: %v", err)
	}

	reg.Gauge("sentinel_go_fetcher_max_concurrency", "Configured worker ceiling.", func() float64 {
		return float64(cfg.maxConcurrency)
	})
	reg.Gauge("sentinel_go_fetcher_rate_limit", "Configured requests per second.", func() float64 {
		return cfg.fetcher.RatePerSecond
	})

	srv := &server{fetcher: f, cfg: cfg}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.healthz)
	mux.HandleFunc("/metrics", srv.metrics)
	mux.HandleFunc("/fetch", srv.fetchOne)
	mux.HandleFunc("/fetch/batch", srv.fetchBatch)

	addr := ":" + cfg.port
	log.Printf("go-fetcher listening on %s (profile=%s, rate=%.1f/s, workers=%d, allowlist=%d hosts)",
		addr, cfg.fetcher.Profile, cfg.fetcher.RatePerSecond, cfg.maxConcurrency, len(cfg.allowedHosts))

	httpSrv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}

type appConfig struct {
	port           string
	maxConcurrency int
	allowedHosts   map[string]bool
	fetcher        fetcher.Config
}

func configFromEnv() (appConfig, error) {
	profile, err := fingerprint.ParseProfile(os.Getenv("FINGERPRINT_PROFILE"))
	if err != nil {
		return appConfig{}, err
	}

	cfg := fetcher.DefaultConfig()
	cfg.Profile = profile
	cfg.Platform = env("FINGERPRINT_PLATFORM", "windows")
	cfg.RatePerSecond = envFloat("RATE_PER_SECOND", cfg.RatePerSecond)
	cfg.Burst = envInt("RATE_BURST", cfg.Burst)
	cfg.Timeout = time.Duration(envInt("REQUEST_TIMEOUT_MS", int(cfg.Timeout.Milliseconds()))) * time.Millisecond

	allowed := parseAllowlist(os.Getenv("FETCH_ALLOWED_HOSTS"))

	return appConfig{
		port:           env("PORT", "8088"),
		maxConcurrency: envInt("MAX_CONCURRENCY", 16),
		allowedHosts:   allowed,
		fetcher:        cfg,
	}, nil
}

func parseAllowlist(raw string) map[string]bool {
	set := map[string]bool{}
	for _, host := range strings.Split(raw, ",") {
		host = strings.TrimSpace(strings.ToLower(host))
		if host == "" {
			continue
		}
		host = strings.TrimPrefix(host, "www.")
		set[host] = true
	}
	return set
}

type server struct {
	fetcher *fetcher.Fetcher
	cfg     appConfig
}

func (s *server) healthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "service": "go-fetcher"})
}

func (s *server) metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	_, _ = w.Write([]byte(reg.Render()))
}

type fetchRequest struct {
	URL string `json:"url"`
}

func (s *server) fetchOne(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, errorBody("POST only"))
		return
	}
	var req fetchRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody("invalid JSON body"))
		return
	}
	if err := s.gate(req.URL); err != nil {
		writeJSON(w, http.StatusForbidden, errorBody(err.Error()))
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), s.cfg.fetcher.Timeout)
	defer cancel()

	result := s.fetcher.Fetch(ctx, req.URL)
	s.record(result)
	writeJSON(w, http.StatusOK, result)
}

type batchRequest struct {
	URLs        []string `json:"urls"`
	Concurrency int      `json:"concurrency"`
}

type batchResponse struct {
	Stats   fetcher.Stats    `json:"stats"`
	Results []fetcher.Result `json:"results"`
}

func (s *server) fetchBatch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, errorBody("POST only"))
		return
	}
	var req batchRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, errorBody("invalid JSON body"))
		return
	}
	if len(req.URLs) == 0 {
		writeJSON(w, http.StatusBadRequest, errorBody("urls is empty"))
		return
	}

	// Reject the whole batch if any URL is outside the allowlist: a partially honoured
	// batch is harder to reason about than a refused one.
	for _, u := range req.URLs {
		if err := s.gate(u); err != nil {
			writeJSON(w, http.StatusForbidden, errorBody(err.Error()))
			return
		}
	}

	concurrency := req.Concurrency
	if concurrency <= 0 || concurrency > s.cfg.maxConcurrency {
		concurrency = s.cfg.maxConcurrency
	}

	pool := fetcher.NewPool(s.fetcher, concurrency, concurrency*2)
	start := time.Now()
	results := pool.RunAll(r.Context(), req.URLs)
	elapsed := time.Since(start)

	for _, res := range results {
		s.record(res)
	}

	writeJSON(w, http.StatusOK, batchResponse{
		Stats:   fetcher.Summarise(results, elapsed),
		Results: results,
	})
}

// gate enforces the allowlist. An empty allowlist means "local development, no gate";
// production sets FETCH_ALLOWED_HOSTS from governance/sources.yml.
func (s *server) gate(rawURL string) error {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" {
		return fmt.Errorf("invalid url %q", rawURL)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return fmt.Errorf("unsupported scheme %q", parsed.Scheme)
	}
	if len(s.cfg.allowedHosts) == 0 {
		return nil
	}
	host := strings.TrimPrefix(strings.ToLower(parsed.Hostname()), "www.")
	if !s.cfg.allowedHosts[host] {
		return fmt.Errorf("host %q is not in FETCH_ALLOWED_HOSTS", host)
	}
	return nil
}

func (s *server) record(r fetcher.Result) {
	countFetched(1)
	countBytes(int64(r.Bytes))
	if r.Error != "" || r.StatusCode >= 400 {
		countFailed(1)
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func errorBody(msg string) map[string]string { return map[string]string{"error": msg} }

func env(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			return parsed
		}
	}
	return fallback
}

func envFloat(key string, fallback float64) float64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if parsed, err := strconv.ParseFloat(v, 64); err == nil {
			return parsed
		}
	}
	return fallback
}
