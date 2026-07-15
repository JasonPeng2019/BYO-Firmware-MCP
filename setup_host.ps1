param(
    [string]$BoardConfigDir = (Join-Path $PSScriptRoot 'boards'),
    [string[]]$BoardConfig = @(),
    [string[]]$BoardId = @(),
    [switch]$SkipUvSync,
    [switch]$SkipHostBootstrap,
    [switch]$EnsureZephyrBuildEnv,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$NordicNrfCommandLineToolsUrl = 'https://nsscprodmedia.blob.core.windows.net/prod/software-and-other-downloads/desktop-software/nrf-command-line-tools/sw/versions-10-x-x/10-24-2/nrf-command-line-tools-10.24.2-x64.exe'
$NordicNrfCommandLineToolsBin = 'C:\Program Files (x86)\Nordic Semiconductor\nrf-command-line-tools\bin'
$Stm32CubeProgrammerBin = 'C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin'

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host ('=' * 60)
    Write-Host "  $Text"
    Write-Host ('=' * 60)
}

function Write-Status {
    param(
        [string]$Level,
        [string]$Message
    )
    Write-Host "  [$Level] $Message"
}

function Invoke-Step {
    param(
        [string]$Description,
        [scriptblock]$Action
    )
    if ($DryRun) {
        Write-Status 'INFO' "DRY RUN: $Description"
        return
    }
    & $Action
}

function Test-CommandExists {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-PathEntryCurrent {
    param([string]$Entry)
    if (-not $Entry) { return }
    $currentEntries = ($env:PATH -split ';') | Where-Object { $_ }
    if ($currentEntries -contains $Entry) { return }
    $env:PATH = ($currentEntries + $Entry) -join ';'
}

function Add-PathEntryUser {
    param([string]$Entry)
    if (-not $Entry) { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @()
    if ($userPath) {
        $entries = $userPath -split ';' | Where-Object { $_ }
    }
    if ($entries -contains $Entry) { return }
    [Environment]::SetEnvironmentVariable('Path', (($entries + $Entry) -join ';'), 'User')
}

function Get-PythonCommand {
    if (Test-CommandExists 'py') { return 'py' }
    if (Test-CommandExists 'python') { return 'python' }
    return $null
}

function Ensure-Winget {
    if (-not (Test-CommandExists 'winget')) {
        throw "winget is required for unattended Windows setup but was not found."
    }
}

function Install-WingetPackage {
    param([string]$PackageId)
    Ensure-Winget
    Invoke-Step "Install $PackageId via winget" {
        & winget install --id $PackageId --exact --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
        if ($LASTEXITCODE -ne 0) {
            throw "winget install failed for $PackageId"
        }
    }
}

function Ensure-Python {
    Write-Section 'Python'
    $pythonCmd = Get-PythonCommand
    if ($pythonCmd) {
        Write-Status 'PASS' "Python launcher found: $pythonCmd"
        return $pythonCmd
    }

    Write-Status 'WARN' 'Python launcher not found - attempting Windows install via winget'
    Install-WingetPackage 'Python.Python.3.12'
    $pythonCmd = Get-PythonCommand
    if (-not $pythonCmd) {
        throw 'Python install completed but no python launcher was found on PATH.'
    }
    Write-Status 'PASS' "Python launcher found after install: $pythonCmd"
    return $pythonCmd
}

function Get-PythonScriptsDir {
    param([string]$PythonCommand)
    $output = & $PythonCommand -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to query Python scripts directory.'
    }
    return $output.Trim()
}

function Ensure-Uv {
    param([string]$PythonCommand)
    Write-Section 'uv'
    if (Test-CommandExists 'uv') {
        Write-Status 'PASS' 'uv already found on PATH'
        return
    }

    Write-Status 'WARN' 'uv not found - attempting install via pip'
    Invoke-Step 'Install uv with pip' {
        & $PythonCommand -m pip install uv
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to install uv with pip.'
        }
    }

    $scriptsDir = Get-PythonScriptsDir -PythonCommand $PythonCommand
    Add-PathEntryCurrent -Entry $scriptsDir
    Add-PathEntryUser -Entry $scriptsDir

    if (-not (Test-CommandExists 'uv')) {
        throw 'uv install completed but uv was not found on PATH.'
    }
    Write-Status 'PASS' "uv installed and PATH repaired via $scriptsDir"
}

function Ensure-UvSync {
    Write-Section 'Repo environment'
    if ($SkipUvSync) {
        Write-Status 'INFO' 'Skipping uv sync by request'
        return
    }
    Invoke-Step "Run 'uv sync --locked'" {
        & uv sync --locked
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync --locked failed."
        }
    }
    Write-Status 'PASS' "Repo environment synced with 'uv sync --locked'"
}

function Get-SelectedBoardsViaLoader {
    Write-Section 'Board config'

    $cmd = @('uv', 'run')
    if ($SkipUvSync -or $DryRun) {
        $cmd += '--no-sync'
    }
    $cmd += @(
        'python',
        '-m',
        'pyocd_debug_mcp.board_config_cli',
        '--board-config-dir',
        $BoardConfigDir
    )
    foreach ($path in $BoardConfig) {
        $cmd += @('--board-config', $path)
    }
    foreach ($id in $BoardId) {
        $cmd += @('--board-id', $id)
    }

    $json = & $cmd[0] @($cmd[1..($cmd.Count - 1)])
    if ($LASTEXITCODE -ne 0) {
        $hint = ''
        if ($SkipUvSync -or $DryRun) {
            $hint = ' The repo environment must already exist when using -SkipUvSync or -DryRun because board metadata is resolved through the shared Python loader.'
        }
        throw "Shared board loader failed.$hint"
    }

    try {
        $boards = @($json | ConvertFrom-Json)
    }
    catch {
        throw "Shared board loader returned invalid JSON: $json"
    }

    if (-not $boards -or $boards.Count -eq 0) {
        throw 'No board configs were selected.'
    }

    Write-Status 'PASS' ("Selected boards via shared loader: " + (($boards | ForEach-Object { $_.board_id }) -join ', '))
    return $boards
}

function Find-ExecutableInRoots {
    param(
        [string[]]$Roots,
        [string]$Filter
    )
    foreach ($root in $Roots) {
        if (-not (Test-Path $root)) { continue }
        $found = Get-ChildItem $root -Recurse -Filter $Filter -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            return $found.FullName
        }
    }
    return $null
}

function Repair-ExecutablePath {
    param(
        [string]$CommandName,
        [string[]]$CandidateFiles
    )
    if (Test-CommandExists $CommandName) {
        Write-Status 'PASS' "$CommandName already found on PATH"
        return $true
    }

    foreach ($candidate in $CandidateFiles) {
        if (Test-Path $candidate) {
            $dir = Split-Path -Parent $candidate
            Add-PathEntryCurrent -Entry $dir
            Add-PathEntryUser -Entry $dir
            if (Test-CommandExists $CommandName) {
                Write-Status 'PASS' "$CommandName found after PATH repair ($dir)"
                return $true
            }
        }
    }
    return $false
}

function Ensure-JLink {
    Write-Section 'SEGGER J-Link'
    $knownPaths = @(
        'C:\Program Files\SEGGER\JLink\JLink.exe',
        (Find-ExecutableInRoots -Roots @('C:\Program Files\SEGGER') -Filter 'JLink.exe')
    ) | Where-Object { $_ }

    if (Repair-ExecutablePath -CommandName 'JLink.exe' -CandidateFiles $knownPaths) {
        return
    }

    Write-Status 'WARN' 'J-Link not found - attempting install via winget'
    Install-WingetPackage 'NordicSemiconductor.JLink'
    if (-not (Repair-ExecutablePath -CommandName 'JLink.exe' -CandidateFiles $knownPaths)) {
        throw 'J-Link install completed but JLink.exe was not found.'
    }
}

function Ensure-NrfjprogOptional {
    Write-Section 'Nordic nRF Command Line Tools (optional helper)'
    $knownPaths = @(
        (Join-Path $NordicNrfCommandLineToolsBin 'nrfjprog.exe'),
        (Find-ExecutableInRoots -Roots @('C:\Program Files', 'C:\Program Files (x86)') -Filter 'nrfjprog.exe')
    ) | Where-Object { $_ }

    if (Repair-ExecutablePath -CommandName 'nrfjprog' -CandidateFiles $knownPaths) {
        Write-Status 'PASS' 'nrfjprog available for Nordic serial auto-detect and fallback flashing'
        return $true
    }

    $installerPath = Join-Path $env:TEMP 'nrf-command-line-tools-10.24.2-x64.exe'
    Write-Status 'WARN' 'nrfjprog not found - attempting optional official Nordic installer download'
    Invoke-Step "Download nRF Command Line Tools installer to $installerPath" {
        Invoke-WebRequest -UseBasicParsing $NordicNrfCommandLineToolsUrl -OutFile $installerPath
    }
    Invoke-Step 'Run nRF Command Line Tools silent installer' {
        $process = Start-Process -FilePath $installerPath -ArgumentList '/S' -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "nRF Command Line Tools installer exited with code $($process.ExitCode)"
        }
    }

    if ($DryRun) {
        Write-Status 'INFO' 'DRY RUN: skipping post-install nrfjprog verification'
        return $true
    }

    Add-PathEntryCurrent -Entry $NordicNrfCommandLineToolsBin
    Add-PathEntryUser -Entry $NordicNrfCommandLineToolsBin

    if (-not (Repair-ExecutablePath -CommandName 'nrfjprog' -CandidateFiles $knownPaths)) {
        Write-Status 'WARN' 'nRF Command Line Tools install did not produce a usable nrfjprog on PATH. Continuing because this helper is optional after normal probe setup.'
        return $false
    }
    Write-Status 'PASS' 'nrfjprog available for Nordic serial auto-detect and fallback flashing'
    return $true
}

function Ensure-Stm32CubeProgrammerPath {
    Write-Section 'STM32CubeProgrammer'
    $knownPaths = @(
        (Join-Path $Stm32CubeProgrammerBin 'STM32_Programmer_CLI.exe'),
        (Find-ExecutableInRoots -Roots @('C:\Program Files\STMicroelectronics', 'C:\Program Files (x86)\STMicroelectronics') -Filter 'STM32_Programmer_CLI.exe')
    ) | Where-Object { $_ }

    if (Repair-ExecutablePath -CommandName 'STM32_Programmer_CLI' -CandidateFiles $knownPaths) {
        return $true
    }

    Write-Status 'WARN' 'STM32_Programmer_CLI not found. This script can repair PATH for an existing install, but it does not yet have a verified unattended ST installer flow.'
    return $false
}

function Run-HostBootstrap {
    param([object[]]$Boards)
    if ($SkipHostBootstrap) {
        Write-Status 'INFO' 'Skipping host bootstrap by request'
        return
    }

    Write-Section 'Host bootstrap'
    $cmd = @('uv', 'run', 'python', 'host_bootstrap.py', '--install-packs')
    foreach ($board in $Boards) {
        $cmd += @('--board-id', $board.board_id)
    }

    Invoke-Step ($cmd -join ' ') {
        $arguments = @()
        if ($cmd.Count -gt 1) {
            $arguments = $cmd[1..($cmd.Count - 1)]
        }
        & $cmd[0] @arguments
        if ($LASTEXITCODE -ne 0) {
            throw 'host_bootstrap.py reported that setup is still incomplete.'
        }
    }
}

function Run-ZephyrBuildBootstrap {
    if (-not $EnsureZephyrBuildEnv) {
        Write-Status 'INFO' 'Skipping Zephyr build bootstrap (use -EnsureZephyrBuildEnv when this host must rebuild firmware locally)'
        return
    }

    Write-Section 'Zephyr build bootstrap'
    $cmd = @('uv', 'run', 'pyocd-zephyr-build', '--ensure-only')
    Invoke-Step ($cmd -join ' ') {
        & $cmd[0] @($cmd[1..($cmd.Count - 1)])
        if ($LASTEXITCODE -ne 0) {
            throw 'pyocd-zephyr-build --ensure-only did not complete successfully.'
        }
    }
}

try {
    Write-Section 'Windows host setup'
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'setup_host.ps1 currently supports Windows host automation only.'
    }

    $boards = @(Get-SelectedBoardsViaLoader)
    if (-not $boards -or @($boards).Count -eq 0) {
        throw 'No board configs were selected.'
    }

    Write-Status 'INFO' ("Selected boards: " + (($boards | ForEach-Object { $_.board_id }) -join ', '))

    $pythonCommand = Ensure-Python
    Ensure-Uv -PythonCommand $pythonCommand
    Ensure-UvSync

    $needsNordicJlink = $boards | Where-Object { $_.mcu_family.StartsWith('nrf') -and $_.probe_family -eq 'jlink' }
    $needsStlink = $boards | Where-Object { $_.probe_family -eq 'stlink' }

    if ($needsNordicJlink) {
        Ensure-JLink
        [void](Ensure-NrfjprogOptional)
    }

    if ($needsStlink) {
        [void](Ensure-Stm32CubeProgrammerPath)
    }

    Run-ZephyrBuildBootstrap
    Run-HostBootstrap -Boards $boards

    Write-Section 'Done'
    Write-Status 'PASS' 'Windows host setup script completed.'
    exit 0
}
catch {
    Write-Section 'Failed'
    Write-Status 'FAIL' $_.Exception.Message
    exit 1
}
