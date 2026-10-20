# Start the go-fetcher, exercise every endpoint, stop it. Prints a PASS/FAIL summary.
$ErrorActionPreference = "Stop"

$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + `
            [System.Environment]::GetEnvironmentVariable("Path", "User")

$bin = Join-Path $env:TEMP "go-fetcher.exe"
$out = Join-Path $env:TEMP "gofetcher.out"
$err = Join-Path $env:TEMP "gofetcher.err"

$env:PORT = "8088"
$env:FINGERPRINT_PROFILE = "chrome"
$env:RATE_PER_SECOND = "20"
$env:RATE_BURST = "10"
$env:MAX_CONCURRENCY = "8"
$env:FETCH_ALLOWED_HOSTS = "books.toscrape.com,quotes.toscrape.com,scrapethissite.com"

$proc = Start-Process -FilePath $bin -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err

$failures = 0
function Check([string]$label, [scriptblock]$body) {
    try {
        $result = & $body
        Write-Host ("  PASS  {0}<br>{1}" -f $label, $result)
    } catch {
        Write-Host ("  FAIL  {0} :: {1}" -f $label, $_.Exception.Message)
        $script:failures++
    }
}

try {
    Start-Sleep -Seconds 3

    Check "healthz" {
        (Invoke-WebRequest "http://127.0.0.1:8088/healthz" -UseBasicParsing -TimeoutSec 10).Content
    }

    Check "metrics exposes counters" {
        $m = (Invoke-WebRequest "http://127.0.0.1:8088/metrics" -UseBasicParsing -TimeoutSec 10).Content
        ($m -split "`n" | Select-String "^sentinel_go_fetcher_" | Measure-Object).Count.ToString() + " metric lines"
    }

    Check "gate refuses undeclared host" {
        try {
            Invoke-WebRequest "http://127.0.0.1:8088/fetch" -Method POST -UseBasicParsing -TimeoutSec 10 `
                -ContentType "application/json" -Body '{"url":"https://example.com"}'
            throw "expected 403"
        } catch {
            $code = $_.Exception.Response.StatusCode.value__
            if ($code -ne 403) { throw "expected 403, got $code" }
            "403 as expected"
        }
    }

    Check "fetch a declared host" {
        $r = Invoke-WebRequest "http://127.0.0.1:8088/fetch" -Method POST -UseBasicParsing -TimeoutSec 40 `
            -ContentType "application/json" -Body '{"url":"https://books.toscrape.com/robots.txt"}'
        $j = $r.Content | ConvertFrom-Json
        # A connection that never completed reports StatusCode 0 and a non-empty Error.
        # Treating "the request returned" as success would call a total failure a pass.
        if ($j.Error) { throw "fetch error: $($j.Error)" }
        if ($j.StatusCode -lt 200 -or $j.StatusCode -ge 500) { throw "unexpected status $($j.StatusCode)" }
        "status=$($j.StatusCode) bytes=$($j.Bytes) attempts=$($j.Attempts)"
    }

    Check "batch across a declared host" {
        $body = '{"urls":["https://books.toscrape.com/robots.txt","https://quotes.toscrape.com/robots.txt"],"concurrency":4}'
        $r = Invoke-WebRequest "http://127.0.0.1:8088/fetch/batch" -Method POST -UseBasicParsing -TimeoutSec 60 `
            -ContentType "application/json" -Body $body
        $j = $r.Content | ConvertFrom-Json
        if ($j.stats.succeeded -lt 1) { throw "no successful fetches: $($j.stats | ConvertTo-Json -Compress)" }
        "total=$($j.stats.total) ok=$($j.stats.succeeded) rps=$([math]::Round($j.stats.requests_per_second,2)) p95=$($j.stats.p95_ms)ms"
    }
}
finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}

Write-Host ""
if ($failures -gt 0) {
    Write-Host "go-fetcher endpoint check: $failures FAILED"
    Get-Content $err -ErrorAction SilentlyContinue | Select-Object -Last 10
    exit 1
}
Write-Host "go-fetcher endpoint check: all passed"
exit 0
