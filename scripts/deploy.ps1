[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z]{2}-[a-z]+-[0-9]+$')]
    [string]$Region,

    [ValidatePattern('^$|^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')]
    [string]$ArtifactBucket = '',

    [ValidatePattern('^[a-zA-Z][a-zA-Z0-9-]{0,50}$')]
    [string]$StackName = 'capital-ledger',

    [ValidatePattern('^$|^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$')]
    [string]$CognitoDomainPrefix = '',

    [ValidatePattern('^$|^16\.[0-9]+(\.[0-9]+)?$')]
    [string]$AuroraEngineVersion = '',

    [string]$ExistingLambdaRoleArn = '',
    [string]$Profile = '',
    [switch]$DisableSchedules
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildDirectory = Join-Path $projectRoot '.build'
$templateFile = Join-Path $projectRoot 'infra/template.yaml'
$packagedTemplate = Join-Path $buildDirectory 'packaged.yaml'
$awsBaseArguments = @('--region', $Region, '--no-cli-pager')
if ($Profile) { $awsBaseArguments += @('--profile', $Profile) }

function Invoke-LedgerAws {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $result = & aws @Arguments @awsBaseArguments
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI command failed: aws $($Arguments[0]) $($Arguments[1])"
    }
    return $result
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw 'Install AWS CLI v2.32 or newer and configure an authorized AWS account before deployment.'
}
$awsCliVersionOutput = (& aws --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $awsCliVersionOutput -notmatch 'aws-cli/([0-9]+\.[0-9]+\.[0-9]+)') {
    throw 'Could not determine the AWS CLI version. Install AWS CLI v2.32 or newer.'
}
$awsCliVersion = [version]$Matches[1]
if ($awsCliVersion -lt [version]'2.32.0') {
    throw "AWS CLI $awsCliVersion is installed. Install v2.32 or newer; the personal-account setup uses aws login."
}

# Identity lookup is read-only. It also fails early when a profile is not signed in.
$callerIdentity = Invoke-LedgerAws -Arguments @('sts', 'get-caller-identity', '--output', 'json') | ConvertFrom-Json
$deploymentAccountId = [string]$callerIdentity.Account
if ($deploymentAccountId -notmatch '^[0-9]{12}$') {
    throw 'AWS returned an invalid account ID. Check the selected AWS profile.'
}
# Aurora on the Free plan supports Express configuration only. This template
# creates a full-configuration VPC cluster, so stop before any resource writes.
$accountPlan = Invoke-LedgerAws -Arguments @('freetier', 'get-account-plan-state', '--output', 'json') | ConvertFrom-Json
if ($accountPlan.accountPlanType -eq 'FREE') {
    throw 'This full-configuration deployment is not compatible with the AWS Free plan. Aurora must be created with Express configuration and the app connected to its supported authentication path. No resources were created by this invocation. Do not upgrade your plan just to bypass this check.'
}
if ($accountPlan.accountPlanType -ne 'PAID') {
    throw 'Could not verify the AWS account plan. Stopping before resource creation.'
}
if (-not $ArtifactBucket) { $ArtifactBucket = "cl-artifacts-$deploymentAccountId-$Region" }
if (-not $CognitoDomainPrefix) {
    $domainStem = $StackName.ToLowerInvariant()
    if ($domainStem.Length -gt 32) { $domainStem = $domainStem.Substring(0, 32) }
    $CognitoDomainPrefix = "$domainStem-$deploymentAccountId"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'backend/ledger/schema.sql'))) {
    throw 'Run this script from the complete Capital Ledger repository; backend/ledger/schema.sql is missing.'
}
New-Item -ItemType Directory -Force -Path $buildDirectory | Out-Null

Write-Host 'Checking the selected AWS account, template, and database availability.'
Invoke-LedgerAws -Arguments @('cloudformation', 'validate-template', '--template-body', "file://$templateFile", '--output', 'json') | Out-Null

# Discover a current available 16.x version instead of pinning a version that can retire.
if (-not $AuroraEngineVersion) {
    $versionJson = Invoke-LedgerAws -Arguments @('rds', 'describe-db-engine-versions', '--engine', 'aurora-postgresql', '--query', "DBEngineVersions[?starts_with(EngineVersion, '16.') && Status == 'available'].EngineVersion", '--output', 'json')
    $versions = @($versionJson | ConvertFrom-Json) |
        Where-Object { $_ -match '^16\.[0-9]+(\.[0-9]+)?$' } |
        Sort-Object { [version]$_ } -Descending
    foreach ($candidate in $versions) {
        $optionJson = Invoke-LedgerAws -Arguments @('rds', 'describe-orderable-db-instance-options', '--engine', 'aurora-postgresql', '--engine-version', $candidate, '--db-instance-class', 'db.serverless', '--query', 'OrderableDBInstanceOptions[].EngineVersion', '--output', 'json')
        if (@($optionJson | ConvertFrom-Json).Count -gt 0) {
            $AuroraEngineVersion = $candidate
            break
        }
    }
    if (-not $AuroraEngineVersion) { throw "No available Aurora PostgreSQL 16.x db.serverless version found in $Region." }
}
Write-Host 'Deployment plan:'
Write-Host "  AWS account: $deploymentAccountId"
Write-Host "  Region: $Region"
Write-Host "  Stack: $StackName"
Write-Host "  Artifact bucket: $ArtifactBucket (created if absent)"
Write-Host "  Cognito domain prefix: $CognitoDomainPrefix"
Write-Host "  Aurora PostgreSQL: $AuroraEngineVersion, minimum 0.5 / maximum 2 ACUs"
Write-Host '  Resources: private Aurora, DynamoDB, Lambda, API Gateway, Cognito, private S3, CloudFront, logs, and schedules.'
Write-Host 'This invocation now creates or updates billable AWS resources. Aurora has a nonzero minimum capacity and ongoing charges until cleanup.'

$deploymentPlan = [ordered]@{
    accountId = $deploymentAccountId
    region = $Region
    stackName = $StackName
    artifactBucket = $ArtifactBucket
    cognitoDomainPrefix = $CognitoDomainPrefix
    auroraEngineVersion = $AuroraEngineVersion
}
$deploymentPlan | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $buildDirectory 'deployment-plan.json') -Encoding utf8

# The package bucket is separate from the stack so the package can exist before deployment.
# Preserve an existing bucket's policies; require it to be owned by this account and private.
$previousErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $headBucketOutput = & aws s3api head-bucket --bucket $ArtifactBucket --expected-bucket-owner $deploymentAccountId @awsBaseArguments 2>&1
    $headBucketExitCode = $LASTEXITCODE
}
finally { $ErrorActionPreference = $previousErrorPreference }
if ($headBucketExitCode -ne 0) {
    $headBucketMessage = $headBucketOutput | Out-String
    if ($headBucketMessage -notmatch '\(404\)|NoSuchBucket|Not Found') {
        throw "Cannot access artifact bucket $ArtifactBucket as account $deploymentAccountId. Check S3 permissions or provide an unused -ArtifactBucket name. $headBucketMessage"
    }
    Write-Host "Creating private artifact bucket $ArtifactBucket."
    $createBucketArguments = @('s3api', 'create-bucket', '--bucket', $ArtifactBucket)
    if ($Region -ne 'us-east-1') {
        $createBucketArguments += @('--create-bucket-configuration', "LocationConstraint=$Region")
    }
    Invoke-LedgerAws -Arguments $createBucketArguments | Out-Null
    Invoke-LedgerAws -Arguments @('s3api', 'wait', 'bucket-exists', '--bucket', $ArtifactBucket, '--expected-bucket-owner', $deploymentAccountId) | Out-Null
    Invoke-LedgerAws -Arguments @('s3api', 'put-public-access-block', '--bucket', $ArtifactBucket, '--expected-bucket-owner', $deploymentAccountId, '--public-access-block-configuration', 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true') | Out-Null
    $encryptionFile = Join-Path $buildDirectory 'artifact-encryption.json'
    @{ Rules = @(@{ ApplyServerSideEncryptionByDefault = @{ SSEAlgorithm = 'AES256' } }) } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $encryptionFile -Encoding utf8
    Invoke-LedgerAws -Arguments @('s3api', 'put-bucket-encryption', '--bucket', $ArtifactBucket, '--expected-bucket-owner', $deploymentAccountId, '--server-side-encryption-configuration', "file://$encryptionFile") | Out-Null
}
else {
    $location = Invoke-LedgerAws -Arguments @('s3api', 'get-bucket-location', '--bucket', $ArtifactBucket, '--expected-bucket-owner', $deploymentAccountId, '--output', 'json') | ConvertFrom-Json
    $bucketRegion = $location.LocationConstraint
    if (-not $bucketRegion) { $bucketRegion = 'us-east-1' }
    if ($bucketRegion -eq 'EU') { $bucketRegion = 'eu-west-1' }
    if ($bucketRegion -ne $Region) {
        throw "Artifact bucket $ArtifactBucket is in $bucketRegion; use a bucket in $Region."
    }
    $accessBlock = Invoke-LedgerAws -Arguments @('s3api', 'get-public-access-block', '--bucket', $ArtifactBucket, '--expected-bucket-owner', $deploymentAccountId, '--output', 'json') | ConvertFrom-Json
    foreach ($setting in @('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')) {
        if (-not $accessBlock.PublicAccessBlockConfiguration.$setting) {
            throw "Existing artifact bucket $ArtifactBucket must have all four S3 public-access-block settings enabled. Supply a new private bucket name or configure this bucket before deploying."
        }
    }
}

# Runtime dependencies are stdlib and AWS Lambda's bundled boto3. No native build is required.
Invoke-LedgerAws -Arguments @('cloudformation', 'package', '--template-file', $templateFile, '--s3-bucket', $ArtifactBucket, '--s3-prefix', "$StackName/backend", '--output-template-file', $packagedTemplate) | Out-Null

$parameterOverrides = @(
    "AuroraEngineVersion=$AuroraEngineVersion",
    "CognitoDomainPrefix=$CognitoDomainPrefix",
    "ExistingLambdaRoleArn=$ExistingLambdaRoleArn",
    'EnableSchedules=false'
)
$deployArguments = @('cloudformation', 'deploy', '--template-file', $packagedTemplate, '--stack-name', $StackName, '--capabilities', 'CAPABILITY_IAM', 'CAPABILITY_AUTO_EXPAND', '--no-fail-on-empty-changeset', '--parameter-overrides') + $parameterOverrides
Invoke-LedgerAws -Arguments $deployArguments | Out-Host

$outputJson = Invoke-LedgerAws -Arguments @('cloudformation', 'describe-stacks', '--stack-name', $StackName, '--query', 'Stacks[0].Outputs', '--output', 'json')
$stackOutputs = @{}
foreach ($outputItem in ($outputJson | ConvertFrom-Json)) { $stackOutputs[$outputItem.OutputKey] = $outputItem.OutputValue }
$stackOutputs | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $buildDirectory 'stack-outputs.json') -Encoding utf8

Write-Host 'Creating the schema with the private bootstrap function.'
$bootstrapResponse = Join-Path $buildDirectory 'bootstrap-response.json'
$bootstrapSucceeded = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    $metadata = Invoke-LedgerAws -Arguments @('lambda', 'invoke', '--function-name', $stackOutputs.BootstrapFunctionName, '--payload', '{}', '--cli-binary-format', 'raw-in-base64-out', '--output', 'json', $bootstrapResponse) | ConvertFrom-Json
    if (-not $metadata.FunctionError) {
        $bootstrapSucceeded = $true
        break
    }
    if ($attempt -lt 5) { Start-Sleep -Seconds 10 }
}
if (-not $bootstrapSucceeded) {
    throw "Schema initialization failed. Review $bootstrapResponse and the bootstrap CloudWatch logs. Schedules remain disabled."
}
Get-Content -LiteralPath $bootstrapResponse | Out-Host

# Browser configuration contains public identifiers only. No AWS credentials or DB secret.
$websiteUrl = $stackOutputs.WebsiteUrl.TrimEnd('/')
$browserConfig = [ordered]@{
    apiBaseUrl = $stackOutputs.ApiBaseUrl
    authMode = 'cognito'
    cognitoDomain = $stackOutputs.CognitoDomain
    cognitoClientId = $stackOutputs.CognitoClientId
    redirectUri = "$websiteUrl/"
    logoutUri = "$websiteUrl/"
    oauthScopes = @('openid', 'email', 'profile', 'ledger-api/access')
}
$configFile = Join-Path $buildDirectory 'config.js'
('window.APP_CONFIG = ' + ($browserConfig | ConvertTo-Json -Depth 4) + ';') | Set-Content -LiteralPath $configFile -Encoding utf8

Write-Host 'Publishing the S3 frontend behind CloudFront HTTPS.'
Invoke-LedgerAws -Arguments @('s3', 'sync', (Join-Path $projectRoot 'frontend'), "s3://$($stackOutputs.FrontendBucketName)/", '--exclude', 'config.js', '--cache-control', 'no-cache') | Out-Host
Invoke-LedgerAws -Arguments @('s3', 'cp', $configFile, "s3://$($stackOutputs.FrontendBucketName)/config.js", '--content-type', 'application/javascript', '--cache-control', 'no-store') | Out-Host

if (-not $DisableSchedules) {
    $parameterOverrides[-1] = 'EnableSchedules=true'
    $scheduleDeployArguments = @('cloudformation', 'deploy', '--template-file', $packagedTemplate, '--stack-name', $StackName, '--capabilities', 'CAPABILITY_IAM', 'CAPABILITY_AUTO_EXPAND', '--no-fail-on-empty-changeset', '--parameter-overrides') + $parameterOverrides
    Invoke-LedgerAws -Arguments $scheduleDeployArguments | Out-Host
}

Write-Host "Website: $websiteUrl/"
Write-Host "API health: $($stackOutputs.ApiBaseUrl)/api/health"
Write-Host 'Sign up through the hosted login, verify your email, and use simulated money only.'
Write-Host "Resource identifiers are saved to $buildDirectory/stack-outputs.json."
