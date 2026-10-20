// Package ratelimit implements a token bucket with no external dependency.
//
// A token bucket, not a fixed sleep: a fixed sleep wastes the whole interval even when
// the work is faster, and produces a burst-free crawl that is slower than a polite one
// with a small burst allowance.
package ratelimit

import (
	"context"
	"sync"
	"time"
)

// Limiter is a token bucket. Safe for concurrent use.
type Limiter struct {
	mu         sync.Mutex
	rate       float64 // tokens per second
	burst      float64 // maximum tokens held
	tokens     float64
	lastRefill time.Time
}

// New returns a limiter that admits `perSecond` requests with a burst allowance.
// A rate of zero means "no limit" and returns a limiter that never blocks.
func New(perSecond float64, burst int) *Limiter {
	if perSecond <= 0 {
		return &Limiter{rate: 0}
	}
	if burst < 1 {
		burst = 1
	}
	return &Limiter{
		rate:       perSecond,
		burst:      float64(burst),
		tokens:     float64(burst),
		lastRefill: time.Now(),
	}
}

// Wait blocks until a token is available or the context is cancelled.
func (l *Limiter) Wait(ctx context.Context) error {
	if l == nil || l.rate <= 0 {
		return nil
	}

	for {
		l.mu.Lock()
		l.refill(time.Now())
		if l.tokens >= 1 {
			l.tokens--
			l.mu.Unlock()
			return nil
		}
		deficit := 1 - l.tokens
		wait := time.Duration(deficit / l.rate * float64(time.Second))
		l.mu.Unlock()

		if wait < time.Millisecond {
			wait = time.Millisecond
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
	}
}

func (l *Limiter) refill(now time.Time) {
	elapsed := now.Sub(l.lastRefill).Seconds()
	if elapsed <= 0 {
		return
	}
	l.tokens += elapsed * l.rate
	if l.tokens > l.burst {
		l.tokens = l.burst
	}
	l.lastRefill = now
}

// Tokens reports the current token count. Used by tests.
func (l *Limiter) Tokens() float64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.refill(time.Now())
	return l.tokens
}
