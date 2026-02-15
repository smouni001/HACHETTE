param(
  [Parameter(Mandatory = $false)]
  [string]$Region = "eu-west-3",

  [Parameter(Mandatory = $false)]
  [string]$AccountId = "",

  [Parameter(Mandatory = $false)]
  [string]$AppName = "idil-papyrus-web",

  [Parameter(Mandatory = $false)]
  [string]$TemplatePath = "web_app/deploy/taskdef.template.json",

  [Parameter(Mandatory = $false)]
  [string]$OutputPath = "web_app/deploy/taskdef.rendered.json"
)

if (-not $AccountId) {
  $AccountId = aws sts get-caller-identity --query Account --output text
}

if (-not (Test-Path $TemplatePath)) {
  throw "Template introuvable: $TemplatePath"
}

$content = Get-Content $TemplatePath -Raw -Encoding UTF8
$content = $content.Replace("__AWS_REGION__", $Region)
$content = $content.Replace("__AWS_ACCOUNT_ID__", $AccountId)
$content = $content.Replace("__APP_NAME__", $AppName)

Set-Content -Path $OutputPath -Value $content -Encoding UTF8
Write-Output "Task definition generee: $OutputPath"

