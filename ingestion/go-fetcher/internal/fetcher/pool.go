package fetcher

import (
	"context"
	"sync"
	"time"
)

// Pool runs fetches with a hard ceiling on concurrency.
//
// Bounded, not "as many goroutines as URLs": unbounded concurrency looks fast in a
// benchmark and looks like an attack to the target, which is how a crawler gets banned
// and how its own memory gets exhausted.
type Pool struct {
	fetcher *Fetcher
	workers int
	queue   chan string
	wg      sync.WaitGroup
	results chan Result
	once    sync.Once
}

// NewPool starts `workers` goroutines reading from a bounded queue.
func NewPool(f *Fetcher, workers, queueSize int) *Pool {
	if workers < 1 {
		workers = 1
	}
	if queueSize < 1 {
		queueSize = workers * 4
	}
	p := &Pool{
		fetcher: f,
		workers: workers,
		queue:   make(chan string, queueSize),
		results: make(chan Result, queueSize),
	}
	p.start()
	return p
}

func (p *Pool) start() {
	for i := 0; i < p.workers; i++ {
		p.wg.Add(1)
		go func() {
			defer p.wg.Done()
			for url := range p.queue {
				ctx, cancel := context.WithTimeout(context.Background(), p.fetcher.cfg.Timeout)
				result := p.fetcher.Fetch(ctx, url)
				cancel()
				p.results <- result
			}
		}()
	}
}

// Submit enqueues a URL. It blocks when the queue is full, which is the backpressure
// signal: the caller should slow down rather than the process growing without bound.
func (p *Pool) Submit(ctx context.Context, url string) error {
	select {
	case p.queue <- url:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close stops accepting work and closes the result channel once every worker drains.
// Call it from a separate goroutine when results are being consumed concurrently.
func (p *Pool) Close() {
	p.once.Do(func() {
		close(p.queue)
		go func() {
			p.wg.Wait()
			close(p.results)
		}()
	})
}

// Results is the channel of completed fetches. It closes after Close and drain.
func (p *Pool) Results() <-chan Result { return p.results }

// RunAll submits every URL, waits for completion and returns the results in submission
// order. Convenient for batches that fit comfortably in memory.
func (p *Pool) RunAll(ctx context.Context, urls []string) []Result {
	out := make([]Result, len(urls))
	var mu sync.Mutex
	var inner sync.WaitGroup
	inner.Add(1)
	go func() {
		defer inner.Done()
		i := 0
		for result := range p.results {
			mu.Lock()
			if i < len(out) {
				out[i] = result
			}
			i++
			mu.Unlock()
		}
	}()

	for _, url := range urls {
		if err := p.Submit(ctx, url); err != nil {
			break
		}
	}
	p.Close()
	inner.Wait()
	return out
}

// Stats summarises a batch. Used by the API response and by the benchmark.
type Stats struct {
	Total      int           `json:"total"`
	Succeeded  int           `json:"succeeded"`
	Failed     int           `json:"failed"`
	Bytes      int           `json:"bytes"`
	Duration   time.Duration `json:"-"`
	DurationMS int64         `json:"duration_ms"`
	RPS        float64       `json:"requests_per_second"`
	P50MS      int64         `json:"p50_ms"`
	P95MS      int64         `json:"p95_ms"`
}

// Summarise computes aggregate numbers for a batch of results.
func Summarise(results []Result, elapsed time.Duration) Stats {
	s := Stats{Total: len(results), Duration: elapsed, DurationMS: elapsed.Milliseconds()}
	latencies := make([]int64, 0, len(results))

	for _, r := range results {
		if r.Error != "" || r.StatusCode == 0 {
			s.Failed++
			continue
		}
		s.Succeeded++
		s.Bytes += r.Bytes
		latencies = append(latencies, r.Elapsed.Milliseconds())
	}
	if elapsed > 0 {
		s.RPS = float64(s.Total) / elapsed.Seconds()
	}
	s.P50MS = percentile(latencies, 50)
	s.P95MS = percentile(latencies, 95)
	return s
}

func percentile(values []int64, p int) int64 {
	if len(values) == 0 {
		return 0
	}
	// Insertion sort: batches in this service are small enough that this is faster than
	// pulling in sort.Slice's interface overhead, and it keeps the helper dependency-free.
	sorted := make([]int64, len(values))
	copy(sorted, values)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j-1] > sorted[j]; j-- {
			sorted[j-1], sorted[j] = sorted[j], sorted[j-1]
		}
	}
	idx := (p * len(sorted)) / 100
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}
