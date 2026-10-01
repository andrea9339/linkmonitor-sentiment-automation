param(
    [int]$MaxLinks = 20,
    [switch]$Headless,
    [switch]$DryRun,
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$PythonExe = "",
    [string]$CodexCmd = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$sentimentSucceeded = $false
$sentimentMessageShown = $false

function Resolve-PythonExe {
    param([string]$Root, [string]$ExplicitPath)

    if ($ExplicitPath) {
        return $ExplicitPath
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    return "python"
}

function Resolve-CodexCmd {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        return $ExplicitPath
    }

    $cmd = Get-Command "codex.cmd" -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $defaultCmd = Join-Path $env:APPDATA "npm\codex.cmd"
    if (Test-Path -LiteralPath $defaultCmd) {
        return $defaultCmd
    }

    throw "codex.cmd non trovato. Installa/verifica Codex CLI prima di usare il test."
}

function Invoke-CheckedCommand {
    param(
        [string]$StepName,
        [string]$FilePath,
        [string[]]$Arguments
    )

    Write-Host "[$(Get-Date -Format s)] START $StepName"
    & $FilePath @Arguments
    $exitCode = $LASTEXITCODE
    if ($null -ne $exitCode -and $exitCode -ne 0) {
        throw "$StepName fallito con exit code $exitCode."
    }
    Write-Host "[$(Get-Date -Format s)] OK $StepName"
}

function Get-NonEmptyLineCount {
    param([string]$Path)

    if (!(Test-Path -LiteralPath $Path)) {
        return 0
    }

    return @(
        Get-Content -LiteralPath $Path -Encoding UTF8 |
            Where-Object { $_.Trim().Length -gt 0 }
    ).Count
}

function Test-DecisionsJsonl {
    param(
        [string]$Path,
        [int]$ExpectedCount
    )

    if (!(Test-Path -LiteralPath $Path)) {
        throw "File decisioni non creato: $Path"
    }

    $allowed = @("positivo", "neutro", "negativo")
    $lines = @(
        Get-Content -LiteralPath $Path -Encoding UTF8 |
            Where-Object { $_.Trim().Length -gt 0 }
    )

    if ($lines.Count -ne $ExpectedCount) {
        throw "Numero decisioni non valido: trovate $($lines.Count), attese $ExpectedCount."
    }

    for ($i = 0; $i -lt $lines.Count; $i++) {
        try {
            $item = $lines[$i] | ConvertFrom-Json
        }
        catch {
            throw "JSON non valido in codex_decisions.jsonl alla riga $($i + 1): $($_.Exception.Message)"
        }

        foreach ($field in @("url", "title", "sentiment", "confidence", "reason")) {
            if (!($item.PSObject.Properties.Name -contains $field)) {
                throw "Campo '$field' mancante in codex_decisions.jsonl alla riga $($i + 1)."
            }
        }

        if ($allowed -notcontains ([string]$item.sentiment).ToLowerInvariant()) {
            throw "Sentiment non valido alla riga $($i + 1): $($item.sentiment)"
        }

        try {
            [void][Convert]::ToDouble($item.confidence, [Globalization.CultureInfo]::InvariantCulture)
        }
        catch {
            throw "Confidence non numerica alla riga $($i + 1): $($item.confidence)"
        }
    }
}

try {
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$PythonExe = Resolve-PythonExe -Root $ProjectRoot -ExplicitPath $PythonExe
$CodexCmd = Resolve-CodexCmd -ExplicitPath $CodexCmd

$logsDir = Join-Path $ProjectRoot "logs"
$archiveDir = Join-Path $logsDir "automation_archive"
$queueFile = Join-Path $logsDir "codex_review_queue.jsonl"
$promptFile = Join-Path $logsDir "codex_sentiment_prompt.md"
$decisionsFile = Join-Path $logsDir "codex_decisions.jsonl"
$botFile = Join-Path $ProjectRoot "linkmonitor_sentiment_bot.py"

New-Item -ItemType Directory -Force -Path $logsDir, $archiveDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$transcriptPath = Join-Path $logsDir "test20_$stamp.log"

$browserMode = if ($Headless) { "--headless" } else { "--headed" }
$applyModeText = if ($DryRun) { "dry-run" } else { "reale" }

Start-Transcript -Path $transcriptPath -Append | Out-Null
try {
    Write-Host "ProjectRoot=$ProjectRoot"
    Write-Host "PythonExe=$PythonExe"
    Write-Host "CodexCmd=$CodexCmd"
    Write-Host "MaxLinks=$MaxLinks"
    Write-Host "BrowserMode=$browserMode"
    Write-Host "Apply=$applyModeText"

    if (!(Test-Path -LiteralPath $botFile)) {
        throw "Script bot non trovato: $botFile"
    }

    if (Test-Path -LiteralPath $queueFile) {
        Copy-Item -LiteralPath $queueFile -Destination (Join-Path $archiveDir "test20_codex_review_queue_$stamp.jsonl") -Force
    }
    if (Test-Path -LiteralPath $decisionsFile) {
        Copy-Item -LiteralPath $decisionsFile -Destination (Join-Path $archiveDir "test20_codex_decisions_$stamp.jsonl") -Force
        Remove-Item -LiteralPath $decisionsFile -Force
    }

    Invoke-CheckedCommand `
        -StepName "Estrazione test LinkMonitor ($MaxLinks link)" `
        -FilePath $PythonExe `
        -Arguments @($botFile, "--extract-for-codex", "--test", $browserMode, "--max-links", "$MaxLinks")

    $queueCount = Get-NonEmptyLineCount -Path $queueFile
    if ($queueCount -le 0) {
        Write-Host "Queue vuota: nessun link da classificare. Stop senza apply."
        $sentimentSucceeded = $true
        exit 0
    }

    if (!(Test-Path -LiteralPath $promptFile)) {
        throw "Prompt Codex non trovato dopo estrazione: $promptFile"
    }

    $codexInstructions = @"
Test LinkMonitor su $MaxLinks link.

Obiettivo:
1. Leggi il prompt operativo sotto.
2. Leggi tutte le righe della queue sotto.
3. Crea o sovrascrivi esclusivamente questo file:
$decisionsFile

Regole operative:
- Scrivi una riga JSON valida per ogni riga della queue, senza righe extra.
- Usa esattamente questi sentiment: positivo, neutro, negativo.
- Non modificare LinkMonitor.
- Non eseguire lo script Python.
- Non modificare altri file del progetto.
- Se devi fare controlli sui link per aumentare confidence, falli tu durante questa sessione Codex.
- Alla fine verifica che il file decisioni abbia $queueCount righe JSONL.

----- INIZIO PROMPT OPERATIVO -----
$(Get-Content -Raw -LiteralPath $promptFile -Encoding UTF8)
----- FINE PROMPT OPERATIVO -----

----- INIZIO QUEUE JSONL -----
$(Get-Content -Raw -LiteralPath $queueFile -Encoding UTF8)
----- FINE QUEUE JSONL -----
"@

    Write-Host "[$(Get-Date -Format s)] START Analisi Codex test ($queueCount link)"
    $codexLastMessage = Join-Path $logsDir "test20_codex_last_message_$stamp.txt"
    $codexInstructions | & $CodexCmd `
        --ask-for-approval never `
        exec `
        --model gpt-6-luna `
        --config model_reasoning_effort=high `
        --skip-git-repo-check `
        --sandbox workspace-write `
        -C $ProjectRoot `
        --output-last-message $codexLastMessage `
        -
    $codexExitCode = $LASTEXITCODE
    if ($null -ne $codexExitCode -and $codexExitCode -ne 0) {
        throw "Analisi Codex test fallita con exit code $codexExitCode."
    }
    Write-Host "[$(Get-Date -Format s)] OK Analisi Codex test"

    Test-DecisionsJsonl -Path $decisionsFile -ExpectedCount $queueCount
    Write-Host "Decisioni validate: $queueCount righe."

    $applyArgs = @($botFile, "--apply-decisions", "--test", $browserMode, "--max-links", "$MaxLinks")
    if ($DryRun) {
        $applyArgs += "--dry-run"
    }

    Invoke-CheckedCommand `
        -StepName "Applicazione sentiment test LinkMonitor ($MaxLinks link, $applyModeText)" `
        -FilePath $PythonExe `
        -Arguments $applyArgs

    $sentimentSucceeded = $true
    Write-Host "[$(Get-Date -Format s)] Test completato."
}
finally {
    if ($sentimentSucceeded) {
        Write-Host "sentiment completato con successo" -ForegroundColor Green
    }
    else {
        Write-Host "sentiment non riuscito" -ForegroundColor Red
    }
    $sentimentMessageShown = $true
    Stop-Transcript | Out-Null
    if ($sentimentSucceeded) {
        Write-Host "AUTOMAZIONE FINITA" -ForegroundColor Green
    }
}
}
catch {
    if (!$sentimentMessageShown) {
        Write-Host "sentiment non riuscito" -ForegroundColor Red
    }
    throw
}
