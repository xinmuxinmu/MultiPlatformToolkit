param([Parameter(Mandatory = $true)][string]$ExePath)

# Load the client's .NET API without invoking its entry point or creating a window.
# Only a sanitized check-in result leaves this process; credentials stay in memory.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$bridgeOutput = [Console]::Out
[Console]::SetOut([IO.TextWriter]::Null)
[Console]::SetError([IO.TextWriter]::Null)
$flags = [Reflection.BindingFlags]'Public,NonPublic,Static'

function Complete-Checkin([bool]$Ok, [string]$ErrorCode, [string]$Message = '') {
    $result = @{ok = $Ok; error = $ErrorCode; message = $Message; already_signed = $script:alreadySigned}
    $bridgeOutput.WriteLine(($result | ConvertTo-Json -Compress))
    $bridgeOutput.Flush()
    if ($Ok) { exit 0 }
    exit 1
}

function Convert-ApiObject($Value) {
    if ($Value -is [string]) { return ConvertFrom-Json -InputObject $Value }
    $json = [Newtonsoft.Json.JsonConvert]::SerializeObject($Value)
    return ConvertFrom-Json -InputObject $json
}

try {
    $script:alreadySigned = $false
    $assembly = [Reflection.Assembly]::LoadFrom($ExePath)
    $handler = $assembly.GetType('Pin.Handler.UserConfigHandler', $true)
    $accountType = $assembly.GetType('Pin.Model.UserConfig', $true)
    # The decoder's name is obfuscated; find it by its stable signature.
    $decoders = @($handler.GetMethods($flags) | Where-Object {
        $parameters = $_.GetParameters()
        $_.ReturnType -eq $accountType -and $parameters.Count -eq 1 -and
            $parameters[0].ParameterType -eq [string]
    })
    if ($decoders.Count -ne 1) { Complete-Checkin $false 'unsupported_client' }
    $paths = @(
        (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'pin\g2'),
        (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'fpark\kf1'),
        (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.uquan\po0')
    )
    $files = @($paths | Where-Object { [IO.File]::Exists($_) } |
        ForEach-Object { Get-Item -LiteralPath $_ } | Sort-Object LastWriteTimeUtc -Descending)
    $account = $null
    foreach ($file in $files) {
        try {
            $candidate = $decoders[0].Invoke($null, @([IO.File]::ReadAllText($file.FullName)))
            if ($null -ne $candidate -and $candidate.Token -and $candidate.DeviceID -and $candidate.ProductID) {
                $account = $candidate
                break
            }
        } catch {
            # Try the next local copy. Never print encrypted or decrypted contents.
        }
    }
    if ($null -eq $account) { Complete-Checkin $false 'local_account_missing' }
    $domainsType = $assembly.GetType('Pin.Handler.DomainHandler', $true)
    $domains = $domainsType.GetMethod('get_Instance', $flags).Invoke($null, @())
    # The GUI normally initializes the domain speed list. Populate it locally
    # instead of starting its background/UI initialization tasks.
    $instanceFlags = [Reflection.BindingFlags]'Public,NonPublic,Instance'
    $speedType = $assembly.GetType('Pin.Handler.DomainSpeed', $true)
    $speedListType = [Collections.Generic.List``1].MakeGenericType($speedType)
    $speedFields = @($domainsType.GetFields($instanceFlags) | Where-Object { $_.FieldType -eq $speedListType })
    if ($speedFields.Count -ne 1) { Complete-Checkin $false 'unsupported_client' }
    $speedList = $speedFields[0].GetValue($domains)
    foreach ($hostName in $domainsType.GetMethod('GetCenterDomain', $instanceFlags).Invoke($domains, @())) {
        $speed = [Activator]::CreateInstance($speedType)
        $speedType.GetProperty('Domain').SetValue($speed, [string]$hostName, $null)
        $speedType.GetProperty('Speed').SetValue($speed, [long]1, $null)
        $speedList.Add($speed)
    }
    if ($domainsType.GetMethod('GetSortedDomain', $instanceFlags).Invoke($domains, @()).Count -eq 0) {
        Complete-Checkin $false 'unsupported_client'
    }
    $httpType = $assembly.GetType('Pin.Tool.HttpUtils', $true)
    $api = $httpType.GetMethod('ApiPost', $flags)
    if ($null -eq $api -or $null -eq $domains) { Complete-Checkin $false 'unsupported_client' }
    $arguments = New-Object object[] 5
    $arguments[0] = $domains
    $arguments[1] = $account
    $arguments[2] = 'daily_sign'
    $arguments[3] = @{}
    $arguments[4] = ''
    $response = $api.Invoke($null, $arguments)
    if ($null -eq $response) {
        Complete-Checkin $false 'request_failed'
    }
    $rpcError = $null
    if ($response.ContainsKey('result')) {
        $response = Convert-ApiObject $response['result']
    } elseif ($response.ContainsKey('error')) {
        $rpcError = Convert-ApiObject $response['error']
    }
    $code = 0
    if ($null -ne $rpcError) {
        $message = [string]$rpcError.message
        if (-not $message -and $rpcError.data -is [string]) { $message = $rpcError.data }
        $code = 1
    } else {
        if ($null -eq $response -or -not ($response.PSObject.Properties.Name -contains 'code') -or
            -not [int]::TryParse([string]$response.code, [ref]$code)) {
            Complete-Checkin $false 'invalid_response'
        }
        $message = [string]$response.msg
    }
    foreach ($secret in @($account.Token, $account.DeviceID, $account.Email)) {
        if ($secret -and $secret.Length -ge 4) { $message = $message.Replace($secret, '[redacted]') }
    }
    $message = ($message -replace '[\r\n\x00-\x1f]', ' ').Trim()
    if ($message.Length -gt 400) { $message = $message.Substring(0, 400) }
    if ($null -eq $rpcError -and $code -eq 109) {
        $script:alreadySigned = $true
        Complete-Checkin $true '' ''
    }
    if ($code -ne 0) { Complete-Checkin $false 'server_error' $message }
    Complete-Checkin $true '' $message
} catch {
    # Exception messages may contain headers or request data. Return a fixed error.
    Complete-Checkin $false 'client_error'
}
