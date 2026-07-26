$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$manifestPath = Join-Path $PSScriptRoot "FINAL_CANDIDATE_MANIFEST.json"
$manifestShaPath = Join-Path $PSScriptRoot "FINAL_CANDIDATE_MANIFEST.sha256"
$manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
$candidateRoot = Join-Path (Split-Path $repo -Parent) ".h00-native-candidates"
$logRoot = Join-Path $PSScriptRoot "native-gates"
$utf8 = New-Object System.Text.UTF8Encoding($false)

if ($manifest.baseline_commit -ne "6f3da0a9a0bb97fb535c8c0ba11a4d2b31f5e876") {
    throw "Unexpected candidate baseline in manifest"
}

New-Item -ItemType Directory -Force -Path $candidateRoot, $logRoot | Out-Null

function Invoke-GateCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Log,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $rendered = "uv " + ($Arguments -join " ")
    [IO.File]::AppendAllText($Log, "`n=== $rendered ===`n", $utf8)
    Push-Location $Candidate
    $previousErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5 surfaces native stderr as a NativeCommandError when
        # ErrorActionPreference is Stop; uv legitimately writes progress there.
        $ErrorActionPreference = "Continue"
        & uv @Arguments 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            [IO.File]::AppendAllText($Log, $line + "`n", $utf8)
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "Gate failed ($exitCode): $rendered; see $Log"
    }
}

function New-VerifiedCandidate {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    $root = [IO.Path]::GetFullPath($candidateRoot)
    $target = [IO.Path]::GetFullPath($Candidate)
    if (-not $target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing candidate outside root: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }

    & git clone --no-local --quiet $repo $target
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed for $target"
    }
    $baseline = (& git -C $target rev-parse HEAD).Trim()
    if ($baseline -ne $manifest.baseline_commit) {
        throw "Wrong baseline in ${target}: $baseline"
    }

    foreach ($property in $manifest.files.PSObject.Properties) {
        $relative = $property.Name
        $source = Join-Path $repo $relative
        $destination = Join-Path $target $relative
        New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $actual = (Get-FileHash -Algorithm SHA256 $destination).Hash.ToLower()
        if ($actual -ne $property.Value) {
            throw "Candidate hash mismatch for $relative"
        }
    }

    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $target "H00_FINAL_CANDIDATE_MANIFEST.json")
    Copy-Item -LiteralPath $manifestShaPath -Destination (Join-Path $target "H00_FINAL_CANDIDATE_MANIFEST.sha256")
    $actualManifestHash = (
        Get-FileHash -Algorithm SHA256 (Join-Path $target "H00_FINAL_CANDIDATE_MANIFEST.json")
    ).Hash.ToLower()
    $expectedManifestHash = (
        Get-Content -Raw (Join-Path $target "H00_FINAL_CANDIDATE_MANIFEST.sha256")
    ).Trim()
    if ($actualManifestHash -ne $expectedManifestHash) {
        throw "Detached candidate manifest hash mismatch"
    }
}

$candidates = @(
    @{ Name = "windows-ordinary"; Directory = "windows-ordinary" },
    @{ Name = "windows-space-unicode"; Directory = "windows candidate spaces µ" }
)

foreach ($entry in $candidates) {
    $candidate = Join-Path $candidateRoot $entry.Directory
    $log = Join-Path $logRoot ($entry.Name + ".log")
    [IO.File]::WriteAllText(
        $log,
        "platform=Windows`ncandidate=$candidate`nmanifest_sha256=$((Get-Content -Raw $manifestShaPath).Trim())`n",
        $utf8
    )
    New-VerifiedCandidate -Candidate $candidate
    $env:UV_LINK_MODE = "copy"
    Invoke-GateCommand $candidate $log @("sync", "--locked")
    Invoke-GateCommand $candidate $log @("lock", "--check")
    Invoke-GateCommand $candidate $log @("build")
    Invoke-GateCommand $candidate $log @("run", "--locked", "--no-sync", "ruff", "check", ".")
    Invoke-GateCommand $candidate $log @("run", "--locked", "--no-sync", "pyright")
    Invoke-GateCommand $candidate $log @("run", "--locked", "--no-sync", "pytest", "--collect-only", "-q")
    Invoke-GateCommand $candidate $log @("run", "--locked", "--no-sync", "pytest", "-q")
    [IO.File]::AppendAllText($log, "`nRESULT=PASS`n", $utf8)
}

Write-Host "Windows native candidate gates: PASS"
