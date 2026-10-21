package fetcher

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/ratelimit"
)

func TestTokenBucketAdmitsBurstThenThrottles(t *testing.T) {
	limiter := ratelimit.New(100, 3) // 100/s, burst 3
	ctx := context.Background()

	start := time.Now()
	for i := 0; i < 3; i++ {
		if err := limiter.Wait(ctx); err != nil {
			t.Fatalf("burst wait %d: %v", i, err)
		}
	}
	burstElapsed := time.Since(start)
	if burstElapsed > 50*time.Millisecond {
		t.Fatalf("burst of 3 should be immediate, took %v", burstElapsed)
	}

	// The 4th must wait for a refill at 100/s, so roughly 10ms.
	if err := limiter.Wait(ctx); err != nil {
		t.Fatalf("post-burst wait: %v", err)
	}
	if waited := time.Since(start); waited < 5*time.Millisecond {
		t.Fatalf("4th token should have been throttled, returned in %v", waited)
	}
}

func TestZeroRateMeansUnlimited(t *testing.T) {
	limiter := ratelimit.New(0, 0)
	start := time.Now()
	for i := 0; i < 1000; i++ {
		if err := limiter.Wait(context.Background()); err != nil {
			t.Fatalf("unlimited wait: %v", err)
		}
	}
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("rate 0 should not block, 1000 waits took %v", elapsed)
	}
}

func TestFetchReturnsBodyAndStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("User-Agent"), "Mozilla/5.0") {
			t.Errorf("expected a browser User-Agent, got %q", r.Header.Get("User-Agent"))
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("hello"))
	}))
	defer server.Close()

	f, err := New(DefaultConfig())
	if err != nil {
		t.Fatalf("new fetcher: %v", err)
	}

	result := f.Fetch(context.Background(), server.URL)
	if result.Error != "" {
		t.Fatalf("unexpected error: %s", result.Error)
	}
	if result.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", result.StatusCode)
	}
	if string(result.Body) != "hello" {
		t.Fatalf("body = %q, want %q", result.Body, "hello")
	}
	if result.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1 for a clean response", result.Attempts)
	}
}

func TestFetchTruncatesOversizedBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(strings.Repeat("x", 5000)))
	}))
	defer server.Close()

	cfg := DefaultConfig()
	cfg.MaxBodyBytes = 100
	f, err := New(cfg)
	if err != nil {
		t.Fatalf("new fetcher: %v", err)
	}

	result := f.Fetch(context.Background(), server.URL)
	if !result.Truncated {
		t.Fatal("expected Truncated=true for a body over MaxBodyBytes")
	}
	if result.Bytes != 100 {
		t.Fatalf("bytes = %d, want 100", result.Bytes)
	}
}

func TestInvalidURLFailsFast(t *testing.T) {
	f, err := New(DefaultConfig())
	if err != nil {
		t.Fatalf("new fetcher: %v", err)
	}
	result := f.Fetch(context.Background(), "://not-a-url")
	if result.Error == "" {
		t.Fatal("expected an error for a malformed URL")
	}
	if result.Attempts != 0 {
		t.Fatalf("a malformed URL should not consume attempts, got %d", result.Attempts)
	}
}

func TestPoolRespectsConcurrencyCeiling(t *testing.T) {
	// Both counters are written by concurrent HTTP handlers, so they are atomic: without
	// this, `go test -race` reports a data race in the test itself. The pool under test was
	// never the problem, but a red gate is red either way, and a race detector that fires on
	// the harness trains people to ignore it.
	var inFlight, peak int64
	release := make(chan struct{})

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		current := atomic.AddInt64(&inFlight, 1)
		for {
			observed := atomic.LoadInt64(&peak)
			if current <= observed || atomic.CompareAndSwapInt64(&peak, observed, current) {
				break
			}
		}
		<-release
		atomic.AddInt64(&inFlight, -1)
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()

	f, err := New(DefaultConfig())
	if err != nil {
		t.Fatalf("new fetcher: %v", err)
	}

	urls := make([]string, 12)
	for i := range urls {
		urls[i] = server.URL
	}

	pool := NewPool(f, 3, 8)
	done := make(chan []Result, 1)
	go func() { done <- pool.RunAll(context.Background(), urls) }()

	time.Sleep(150 * time.Millisecond)
	close(release)
	results := <-done

	if len(results) != len(urls) {
		t.Fatalf("got %d results, want %d", len(results), len(urls))
	}
	observedPeak := atomic.LoadInt64(&peak)
	if observedPeak > 3 {
		t.Fatalf("concurrency ceiling breached: peak = %d, want <= 3", observedPeak)
	}
}

func TestSummariseCountsFailures(t *testing.T) {
	results := []Result{
		{StatusCode: 200, Bytes: 100, Elapsed: 10 * time.Millisecond},
		{StatusCode: 200, Bytes: 200, Elapsed: 20 * time.Millisecond},
		{StatusCode: 404, Bytes: 0, Elapsed: 5 * time.Millisecond, Error: "status 404"},
	}
	stats := Summarise(results, time.Second)

	if stats.Total != 3 || stats.Succeeded != 2 || stats.Failed != 1 {
		t.Fatalf("stats = %+v, want total=3 succeeded=2 failed=1", stats)
	}
	if stats.Bytes != 300 {
		t.Fatalf("bytes = %d, want 300", stats.Bytes)
	}
	if stats.RPS != 3 {
		t.Fatalf("rps = %v, want 3", stats.RPS)
	}
}

// BenchmarkFetch measures sustained throughput against a local server. It is the number
// quoted in the README, so it is produced by a command, not by an estimate.
func BenchmarkFetch(b *testing.B) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(strings.Repeat("payload ", 128)))
	}))
	defer server.Close()

	cfg := DefaultConfig()
	cfg.RatePerSecond = 0 // measure the transport, not the limiter
	cfg.MaxIdleConns = 256
	f, err := New(cfg)
	if err != nil {
		b.Fatalf("new fetcher: %v", err)
	}

	ctx := context.Background()
	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if r := f.Fetch(ctx, server.URL); r.Error != "" {
				b.Fatalf("fetch: %s", r.Error)
			}
		}
	})
}
