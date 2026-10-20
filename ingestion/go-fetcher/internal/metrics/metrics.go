// Package metrics exposes Prometheus text format without pulling in the client library.
//
// The exposition format is four lines of text. A dependency that brings in a full
// instrumentation framework to print them is not a trade this service needs to make.
package metrics

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
)

// Registry holds counters and gauges for one process.
type Registry struct {
	mu       sync.RWMutex
	counters map[string]*int64
	gauges   map[string]func() float64
	help     map[string]string
}

// New returns an empty registry.
func New() *Registry {
	return &Registry{
		counters: map[string]*int64{},
		gauges:   map[string]func() float64{},
		help:     map[string]string{},
	}
}

// Counter registers a monotonically increasing counter and returns an add function.
func (r *Registry) Counter(name, help string) func(delta int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.counters[name]; !ok {
		var v int64
		r.counters[name] = &v
		r.help[name] = help
	}
	ptr := r.counters[name]
	return func(delta int64) { atomic.AddInt64(ptr, delta) }
}

// Gauge registers a value read at scrape time.
func (r *Registry) Gauge(name, help string, read func() float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.gauges[name] = read
	r.help[name] = help
}

// Render produces the Prometheus text exposition.
func (r *Registry) Render() string {
	r.mu.RLock()
	defer r.mu.RUnlock()

	names := make([]string, 0, len(r.counters)+len(r.gauges))
	for name := range r.counters {
		names = append(names, name)
	}
	for name := range r.gauges {
		names = append(names, name)
	}
	sort.Strings(names)

	var b strings.Builder
	for _, name := range names {
		if help, ok := r.help[name]; ok && help != "" {
			fmt.Fprintf(&b, "# HELP %s %s\n", name, help)
		}
		kind := "gauge"
		if _, ok := r.counters[name]; ok {
			kind = "counter"
		}
		fmt.Fprintf(&b, "# TYPE %s %s\n", name, kind)

		if ptr, ok := r.counters[name]; ok {
			fmt.Fprintf(&b, "%s %d\n", name, atomic.LoadInt64(ptr))
			continue
		}
		fmt.Fprintf(&b, "%s %g\n", name, r.gauges[name]())
	}
	return b.String()
}
