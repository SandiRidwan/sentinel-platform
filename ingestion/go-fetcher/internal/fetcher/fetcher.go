// Package fetcher is the high-volume HTTP path.
//
// It exists because the Python crawler is the right tool for a handful of sources and the
// wrong tool for a million requests: connection reuse, bounded concurrency and a real TLS
// fingerprint are easier to hold steady in Go.
package fetcher

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"time"

	utls "github.com/refraction-networking/utls"

	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/fingerprint"
	"github.com/SandiRidwan/sentinel-platform/ingestion/go-fetcher/internal/ratelimit"
)

// Config controls one fetcher instance.
type Config struct {
	Profile          fingerprint.Profile
	Platform         string
	Timeout          time.Duration
	MaxBodyBytes     int64
	MaxIdleConns     int
	IdleConnTimeout  time.Duration
	RatePerSecond    float64
	Burst            int
	FollowRedirects  bool
	InsecureSkipCert bool
	ExtraHeaders     map[string]string
}

// DefaultConfig is tuned for many small requests to a small number of hosts.
func DefaultConfig() Config {
	return Config{
		Profile:         fingerprint.ChromeAuto,
		Platform:        "windows",
		Timeout:         20 * time.Second,
		MaxBodyBytes:    8 << 20, // 8 MiB
		MaxIdleConns:    128,
		IdleConnTimeout: 90 * time.Second,
		RatePerSecond:   5,
		Burst:           10,
		FollowRedirects: true,
	}
}

// Result is one fetched response.
type Result struct {
	URL        string
	StatusCode int
	Body       []byte
	Bytes      int
	Elapsed    time.Duration
	Attempts   int
	Truncated  bool
	Error      string
}

// Fetcher fetches URLs under a rate limit and a browser TLS fingerprint.
type Fetcher struct {
	cfg     Config
	client  *http.Client
	limiter *ratelimit.Limiter
	ua      string
}

// New builds a fetcher, including a transport that presents the configured ClientHello.
func New(cfg Config) (*Fetcher, error) {
	if cfg.Timeout <= 0 {
		cfg.Timeout = DefaultConfig().Timeout
	}
	if cfg.MaxBodyBytes <= 0 {
		cfg.MaxBodyBytes = DefaultConfig().MaxBodyBytes
	}

	dial := dialerFor(cfg)

	transport := &http.Transport{
		DialTLSContext:      dial,
		MaxIdleConns:        cfg.MaxIdleConns,
		MaxIdleConnsPerHost: cfg.MaxIdleConns / 2,
		IdleConnTimeout:     cfg.IdleConnTimeout,
		ForceAttemptHTTP2:   false, // ALPN is negotiated inside the utls handshake
		TLSHandshakeTimeout: cfg.Timeout,
		DisableCompression:  false,
	}

	client := &http.Client{
		Transport: transport,
		Timeout:   cfg.Timeout,
	}
	if !cfg.FollowRedirects {
		client.CheckRedirect = func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}
	}

	return &Fetcher{
		cfg:     cfg,
		client:  client,
		limiter: ratelimit.New(cfg.RatePerSecond, cfg.Burst),
		ua:      cfg.Profile.UserAgentFor(cfg.Platform),
	}, nil
}

// dialerFor returns a DialTLSContext func that performs the handshake with utls.
//
// The plain-Go profile is kept as a control: it lets a benchmark show what the fingerprint
// is actually worth on a given target instead of asserting that it matters.
func dialerFor(cfg Config) func(ctx context.Context, network, addr string) (net.Conn, error) {
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		host, _, err := net.SplitHostPort(addr)
		if err != nil {
			host = addr
		}

		nd := &net.Dialer{Timeout: cfg.Timeout, KeepAlive: 30 * time.Second}
		raw, err := nd.DialContext(ctx, network, addr)
		if err != nil {
			return nil, err
		}

		if cfg.Profile == fingerprint.GoDefault {
			tlsConn := tls.Client(raw, &tls.Config{
				ServerName:         host,
				InsecureSkipVerify: cfg.InsecureSkipCert,
				MinVersion:         tls.VersionTLS12,
			})
			if err := tlsConn.HandshakeContext(ctx); err != nil {
				_ = raw.Close()
				return nil, err
			}
			return tlsConn, nil
		}

		// Narrow ALPN to HTTP/1.1 on the spec itself: utls ignores Config.NextProtos
		// when a ClientHelloID is set, and an h2 negotiation breaks Go's transport.
		// buildSpec runs once per connection; the spec is cheap to rebuild.
		spec, specErr := cfg.Profile.SpecHTTP11()
		if specErr != nil {
			_ = raw.Close()
			return nil, specErr
		}

		uconn := utls.UClient(raw, &utls.Config{
			ServerName:         host,
			InsecureSkipVerify: cfg.InsecureSkipCert,
			MinVersion:         tls.VersionTLS12,
		}, utls.HelloCustom)
		if err := uconn.ApplyPreset(spec); err != nil {
			_ = raw.Close()
			return nil, fmt.Errorf("apply clienthello preset: %w", err)
		}
		if err := uconn.HandshakeContext(ctx); err != nil {
			_ = raw.Close()
			return nil, err
		}
		return uconn, nil
	}
}

// Fetch retrieves one URL. Transport failures are retried on a fresh connection; HTTP
// error statuses are returned, not retried, because a 404 will be a 404 again.
func (f *Fetcher) Fetch(ctx context.Context, rawURL string) Result {
	start := time.Now()
	result := Result{URL: rawURL}

	parsed, err := url.Parse(rawURL)
	if err != nil {
		result.Error = fmt.Sprintf("bad url: %v", err)
		result.Elapsed = time.Since(start)
		return result
	}

	const maxAttempts = 3
	var lastErr error

	for attempt := 1; attempt <= maxAttempts; attempt++ {
		result.Attempts = attempt

		if err := f.limiter.Wait(ctx); err != nil {
			result.Error = fmt.Sprintf("rate limiter: %v", err)
			result.Elapsed = time.Since(start)
			return result
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, parsed.String(), nil)
		if err != nil {
			result.Error = fmt.Sprintf("build request: %v", err)
			result.Elapsed = time.Since(start)
			return result
		}
		f.decorate(req, parsed)

		resp, err := f.client.Do(req)
		if err != nil {
			lastErr = err
			if ctx.Err() != nil {
				break
			}
			// Fresh connection on the next attempt: a broken handshake should not be
			// retried on the same socket.
			f.client.CloseIdleConnections()
			time.Sleep(time.Duration(attempt*150) * time.Millisecond)
			continue
		}

		body, truncated, readErr := readLimited(resp.Body, f.cfg.MaxBodyBytes)
		_ = resp.Body.Close()

		result.StatusCode = resp.StatusCode
		result.Body = body
		result.Bytes = len(body)
		result.Truncated = truncated
		result.Elapsed = time.Since(start)

		if readErr != nil {
			result.Error = fmt.Sprintf("read body: %v", readErr)
		}
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("status %d", resp.StatusCode)
			time.Sleep(time.Duration(attempt*250) * time.Millisecond)
			continue
		}
		return result
	}

	if lastErr != nil {
		result.Error = lastErr.Error()
	}
	result.Elapsed = time.Since(start)
	return result
}

func (f *Fetcher) decorate(req *http.Request, parsed *url.URL) {
	req.Header.Set("User-Agent", f.ua)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Accept-Encoding", "gzip, deflate")
	req.Header.Set("Connection", "keep-alive")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	for k, v := range f.cfg.ExtraHeaders {
		req.Header.Set(k, v)
	}
}

func readLimited(r io.Reader, limit int64) (body []byte, truncated bool, err error) {
	limited := io.LimitReader(r, limit+1)
	data, readErr := io.ReadAll(limited)
	if readErr != nil {
		return data, false, readErr
	}
	if int64(len(data)) > limit {
		return data[:limit], true, nil
	}
	return data, false, nil
}

// ErrNoPool indicates a fetch was requested without a pool.
var ErrNoPool = errors.New("fetcher: no worker pool configured")
