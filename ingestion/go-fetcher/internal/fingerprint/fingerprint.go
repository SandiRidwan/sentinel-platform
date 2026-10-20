// Package fingerprint provides TLS client fingerprints for the fetch path.
//
// Why this exists: Go's crypto/tls sends a ClientHello that no browser sends. Sites that
// fingerprint TLS (JA3/JA4) see it immediately and block. utls lets the process present a
// real browser's ClientHello while keeping Go's HTTP stack above it.
//
// This is the same problem solved on the Python side with curl_cffi's impersonate option;
// here it is solved in Go because this service carries the high-volume path.
package fingerprint

import (
	"fmt"
	"strings"

	utls "github.com/refraction-networking/utls"
)

// Profile names a browser ClientHello this service can present.
type Profile string

const (
	ChromeAuto  Profile = "chrome"
	FirefoxAuto Profile = "firefox"
	SafariAuto  Profile = "safari"
	EdgeAuto    Profile = "edge"
	GoDefault   Profile = "go" // no impersonation; useful as an A/B control
)

// ParseProfile maps a config string to a Profile, defaulting to ChromeAuto.
func ParseProfile(raw string) (Profile, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", "chrome", "chrome_auto":
		return ChromeAuto, nil
	case "firefox", "firefox_auto":
		return FirefoxAuto, nil
	case "safari", "safari_auto":
		return SafariAuto, nil
	case "edge", "edge_auto":
		return EdgeAuto, nil
	case "go", "none", "default":
		return GoDefault, nil
	default:
		return "", fmt.Errorf("unknown fingerprint profile %q (want chrome, firefox, safari, edge or go)", raw)
	}
}

// HelloID returns the utls ClientHello identifier for a profile.
func (p Profile) HelloID() utls.ClientHelloID {
	switch p {
	case FirefoxAuto:
		return utls.HelloFirefox_Auto
	case SafariAuto:
		return utls.HelloSafari_Auto
	case EdgeAuto:
		return utls.HelloEdge_Auto
	case GoDefault:
		return utls.HelloGolang
	default:
		return utls.HelloChrome_Auto
	}
}

// UserAgentFor returns a User-Agent consistent with the TLS profile.
//
// The fingerprint and the User-Agent must agree. A Chrome ClientHello sending a Firefox
// User-Agent is a stronger bot signal than either one alone, so they are chosen together
// rather than configured separately.
func (p Profile) UserAgentFor(platform string) string {
	switch p {
	case FirefoxAuto:
		return "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
	case SafariAuto:
		return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15"
	case EdgeAuto:
		return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
	case GoDefault:
		return "sentinel-go-fetcher/1.0"
	default:
		if strings.EqualFold(platform, "macos") {
			return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
		}
		return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
	}
}

// ALPN returns the protocol list a browser of this profile would offer.
func (p Profile) ALPN() []string {
	switch p {
	case SafariAuto:
		return []string{"h2", "http/1.1"}
	case GoDefault:
		return []string{"http/1.1"}
	default:
		return []string{"h2", "http/1.1"}
	}
}

// SpecHTTP11 returns the profile's ClientHello with ALPN reduced to HTTP/1.1.
//
// Why this is needed: utls.UClient with a ClientHelloID ignores Config.NextProtos and
// sends the profile's own ALPN list, which offers "h2". Go's http.Transport speaks
// HTTP/1.x over a custom DialTLSContext, so an h2 negotiation arrives as a binary
// SETTINGS frame that the transport reports as a malformed HTTP response. Narrowing the
// ALPN on the spec itself is the only place the change takes effect.
//
// The ALPN extension is kept (not deleted) because Chrome always sends one; an absent
// ALPN is its own signal.
func (p Profile) SpecHTTP11() (*utls.ClientHelloSpec, error) {
	spec, err := utls.UTLSIdToSpec(p.HelloID())
	if err != nil {
		return nil, fmt.Errorf("build spec for %s: %w", p, err)
	}
	for i, ext := range spec.Extensions {
		if alpn, ok := ext.(*utls.ALPNExtension); ok {
			alpn.AlpnProtocols = []string{"http/1.1"}
			spec.Extensions[i] = alpn
		}
	}
	return &spec, nil
}
