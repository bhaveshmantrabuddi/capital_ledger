param([int]$Port = 8787, [string]$DatabaseUrl = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'backend'
    if ($DatabaseUrl) {
        python -m ledger.local_server --port $Port --postgres $DatabaseUrl
    } else {
        python -m ledger.local_server --port $Port
    }
} finally {
    $env:PYTHONPATH = $priorPythonPath
}
