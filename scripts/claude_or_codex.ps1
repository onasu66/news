param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PromptParts
)

$ErrorActionPreference = "Stop"

$promptText = ($PromptParts -join " ").Trim()
if (-not $promptText -and -not [Console]::IsInputRedirected) {
    Write-Error "Usage: scripts\claude_or_codex.ps1 <prompt>  OR  Get-Content prompt.txt | scripts\claude_or_codex.ps1"
    exit 2
}
if (-not $promptText) {
    $promptText = [Console]::In.ReadToEnd()
}

$claudeCmd = if ($env:CLAUDE_CODE_CMD) { $env:CLAUDE_CODE_CMD } else { "claude" }
$codexCmd = if ($env:CODEX_CLI_CMD) { $env:CODEX_CLI_CMD } else { "codex" }

function Test-ClaudeUsageLimit {
    param([string]$Text)
    $lower = $Text.ToLowerInvariant()
    $markers = @(
        "usage limit",
        "usage_limit",
        "weekly limit",
        "hit your weekly limit",
        "monthly limit",
        "daily limit",
        "rate limit",
        "rate_limit",
        "quota",
        "too many requests",
        "429",
        "limit exceeded",
        "exceeded your current quota",
        "insufficient credits",
        "claude ai usage limit reached"
    )
    foreach ($marker in $markers) {
        if ($lower.Contains($marker)) {
            return $true
        }
    }
    return $false
}

$claudeOutput = $promptText | & $claudeCmd -p --input-format text 2>&1
$claudeCode = $LASTEXITCODE
$claudeText = ($claudeOutput | Out-String)

if ($claudeCode -eq 0) {
    Write-Output $claudeOutput
    exit 0
}

if (Test-ClaudeUsageLimit $claudeText) {
    Write-Warning "Claude usage limit detected. Retrying the same prompt with Codex CLI."
    $codexOutput = $promptText | & $codexCmd exec --sandbox workspace-write 2>&1
    $codexCode = $LASTEXITCODE
    Write-Output $codexOutput
    exit $codexCode
}

Write-Output $claudeOutput
exit $claudeCode
