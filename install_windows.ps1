[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SkillsDir = $(if ($env:CLAUDE_SKILLS_DIR) { $env:CLAUDE_SKILLS_DIR } elseif ($env:CLAUDE_CONFIG_DIR) { "$env:CLAUDE_CONFIG_DIR/skills" } else { "$env:USERPROFILE/.claude/skills" }),
    [string]$BinDir = $(if ($env:BATON_BIN_DIR) { $env:BATON_BIN_DIR } else { "$env:USERPROFILE/.local/bin" })
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
chcp 65001 > $null

# v2 Windows supports Codex execution and monitoring, with local memory.
# Check only: never install dependencies or invoke a model during setup.
$codex = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
if (-not $codex) { throw 'Native codex.exe is required on PATH (npm .cmd/.ps1 shims are not supported).' }
$python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
$git = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue
if (-not $python -or -not $git) { throw 'Python and Git for Windows must be installed and on PATH.' }
Write-Output "Codex: $($codex.Source)"
Write-Output 'Windows defaults: executor.backend=codex; memory.backend=local; monitor via Codex.'
Write-Output 'Optional Hermes executor: supported without a sandbox on Windows. Set executor.backend="hermes", executor.sandbox_exec=false, and executor.hermes_bin to a native hermes.exe; Hermes runs with --yolo and can read/write any local file.'
Write-Output 'Monitor on Windows: monitor.backend="codex" only; optional monitor.provider (for example deepseek) keeps Codex read-only sandboxing. monitor.backend="hermes" is rejected.'
Write-Output 'Hermes login (user-run, not by this installer): hermes -p baton-exec-v2 auth add <provider> --type oauth'
$ollama = Get-Command ollama.exe -CommandType Application -ErrorAction SilentlyContinue
if ($ollama) { Write-Output "Optional Ollama: $($ollama.Source); mem0 also needs a separately configured memory environment." }
else { Write-Output 'Optional Ollama/mem0: absent; local ledger/keyword memory remains available.' }
$hermes = Get-Command hermes.exe -CommandType Application -ErrorAction SilentlyContinue
if ($hermes) { Write-Output "Optional native Hermes: $($hermes.Source)" }
else { Write-Output 'Optional native Hermes: absent; Hermes executor remains optional. Do not use .cmd/.bat/.ps1 launchers.' }

# Native Windows junctions need neither elevation nor Developer Mode.
$source = (Resolve-Path -LiteralPath "$PSScriptRoot/skills/baton").Path
$script = "$source/scripts/baton.py".Replace('\', '/')
$destination = [IO.Path]::GetFullPath("$SkillsDir/baton")
$bin = [IO.Path]::GetFullPath($BinDir)
$utf8 = [System.Text.UTF8Encoding]::new($false)
$quote = "'"
$shPath = $quote + $script.Replace($quote, "'" + '"' + "'" + '"' + "'") + $quote
$wrappers = @{
    "$bin/baton" = "#!/bin/sh`nexec python $shPath " + '"$@"' + "`n"
    "$bin/baton.cmd" = "@echo off`r`nsetlocal DisableDelayedExpansion`r`nchcp 65001 >nul`r`npython `"$($script.Replace('%', '%%'))`" %*`r`nexit /b %errorlevel%`r`n"
}

# Preflight ALL conflicts before writing anything. Reruns only accept this exact target/content.
$existing = Get-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.LinkType -ne 'Junction' -or
        [IO.Path]::GetFullPath([string]$existing.Target[0]).TrimEnd('\') -ne $source.TrimEnd('\')) {
        throw "Conflict: $destination is not a junction to $source; nothing changed."
    }
}
foreach ($path in $wrappers.Keys) {
    $entry = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($entry -and ($entry.PSIsContainer -or $entry.LinkType -or
        [IO.File]::ReadAllText($path, $utf8) -cne $wrappers[$path])) {
        throw "Conflict: $path is a different file/link/directory; nothing changed."
    }
}
if ($PSCmdlet.ShouldProcess("$destination and $bin", 'Install Baton junction and CLI wrappers')) {
    New-Item -ItemType Directory -Force -Path $SkillsDir, $bin | Out-Null
    if (-not $existing) {
        New-Item -ItemType Junction -Path $destination -Target $source | Out-Null
    }
    foreach ($path in $wrappers.Keys) {
        if (-not (Test-Path -LiteralPath $path)) {
            # CreateNew prevents overwriting a file that appeared after the preflight.
            $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try {
                $bytes = $utf8.GetBytes($wrappers[$path])
                $stream.Write($bytes, 0, $bytes.Length)
            } finally { $stream.Dispose() }
        }
    }
    Write-Output "skill $destination -> $source"
    Write-Output "CLI $bin/baton and $bin/baton.cmd"
}
