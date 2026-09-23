# Run in Windows PowerShell 5.1+ as the desktop user; no Python or Espressif app.
# Errors are printed as one plain line. The window is never closed (no `exit`),
# because this script usually runs inside the user's own session via `irm | iex`.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $global:LASTEXITCODE = 0
    $temporary = $null
    try {
        if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use install.sh on macOS/Linux.' }
        $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        if ($architecture -ne 'AMD64') { throw 'The Windows release currently requires x64.' }
        if ([Environment]::OSVersion.Version.Build -lt 22000) { throw 'Windows 11 or later is required.' }
        $data = Join-Path $env:LOCALAPPDATA 'Sweetmeter'
        $state = Join-Path $data 'state'
        $errorFile = Join-Path $data 'install-error.txt'
        $resultFile = Join-Path $data 'install-result.txt'
        $installedRoot = Join-Path $env:LOCALAPPDATA 'Programs\Sweetmeter'
        $installed = Join-Path $installedRoot 'Sweetmeter.exe'
        $versionPattern = '^[0-9]{4}\.[1-9][0-9]?\.[1-9][0-9]*$'
        $hasInstalled = Test-Path -LiteralPath $installed
        $installedVersion = $null
        if ($hasInstalled) {
            try {
                $raw = [string](Get-Content -LiteralPath (Join-Path $installedRoot '_internal\VERSION') -TotalCount 1 -ErrorAction Stop)
                if ($raw.Trim() -cmatch $versionPattern) { $installedVersion = $raw.Trim() }
            } catch { $installedVersion = $null }
        }
        # The installed copy checks itself (self-test and files), repairs its
        # login startup and opens. Its one-line outcome is shown either way.
        function Invoke-ExistingCheck {
            $null = New-Item -ItemType Directory -Path $state -Force
            $null = New-Item -ItemType File -Path (Join-Path $state 'show-window') -Force
            Remove-Item -LiteralPath $errorFile, $resultFile -Force -ErrorAction SilentlyContinue
            $check = Start-Process -FilePath $installed -ArgumentList '--install' -PassThru
            $check.WaitForExit()
            if ($check.ExitCode -eq 0) {
                if (Test-Path -LiteralPath $resultFile) { Write-Host (Get-Content -LiteralPath $resultFile -Raw).Trim() }
                return $true
            }
            if (Test-Path -LiteralPath $errorFile) { Write-Host (Get-Content -LiteralPath $errorFile -Raw).Trim() }
            return $false
        }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $temporary = Join-Path ([IO.Path]::GetTempPath()) ('sweetmeter-' + [Guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $temporary
        Write-Host 'Finding the latest Sweetmeter release...'
        # Like install.sh: follow github.com's releases/latest redirect, which is
        # not subject to the anonymous REST API rate limit. The API is a fallback.
        $version = $null
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Method Head -Uri 'https://github.com/luvxinc/Sweetmeter/releases/latest' -TimeoutSec 60
            $final = $null
            if ($response.BaseResponse.ResponseUri) { $final = $response.BaseResponse.ResponseUri.AbsoluteUri }
            elseif ($response.BaseResponse.RequestMessage) { $final = $response.BaseResponse.RequestMessage.RequestUri.AbsoluteUri }
            if ($final -and $final -cmatch '^https://github\.com/luvxinc/Sweetmeter/releases/tag/([^/?#]+)$') { $version = $Matches[1] }
        } catch { $version = $null }
        if (-not $version -or $version -cnotmatch $versionPattern) {
            try {
                $releaseInfo = Invoke-RestMethod -Uri 'https://api.github.com/repos/luvxinc/Sweetmeter/releases/latest' -TimeoutSec 60
            } catch {
                if (-not $hasInstalled) { throw }
                Write-Host 'Could not reach GitHub to check for a newer Sweetmeter; checking the installed copy instead.'
                if (Invoke-ExistingCheck) { return }
                throw 'The installed Sweetmeter needs repair (reason above), but the latest release could not be downloaded. Check the Internet connection and run this again.'
            }
            if ($releaseInfo.draft -or $releaseInfo.prerelease) { throw 'The latest release is not a stable release.' }
            $version = $releaseInfo.tag_name
        }
        if ($version -cnotmatch $versionPattern) { throw 'Unexpected release version.' }
        if ($installedVersion -and ([Version]$version -le [Version]$installedVersion)) {
            # The installed copy is current (or newer): keep it when it is healthy.
            if (Invoke-ExistingCheck) { return }
            Write-Host "Reinstalling Sweetmeter $version from the verified release."
        } elseif ($hasInstalled) {
            if ($installedVersion) { Write-Host "Updating your installed Sweetmeter $installedVersion to $version." }
            else { Write-Host "Updating your installed Sweetmeter to $version." }
        }
        $release = "https://github.com/luvxinc/Sweetmeter/releases/download/$version"
        $manifestFile = Join-Path $temporary 'manifest.json'
        $signatureFile = Join-Path $temporary 'manifest.json.sig'
        Invoke-WebRequest -UseBasicParsing -Uri "$release/manifest.json" -OutFile $manifestFile -TimeoutSec 60
        Invoke-WebRequest -UseBasicParsing -Uri "$release/manifest.json.sig" -OutFile $signatureFile -TimeoutSec 60

        # Pinned P-256 public key, identical to meter/assets/keys/release-1.pem.
        $der = [Convert]::FromBase64String('MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE0+b2/kjA0meIfP8wBfk5vVRLx382PMEfiRqKBmAynh8tlOM996Sio32vEQvftl6LdC/GV8x5/ZDdQSxVMxw/tA==')
        [byte[]]$blob = @(0x45, 0x43, 0x53, 0x31, 32, 0, 0, 0) + $der[($der.Length - 64)..($der.Length - 1)]
        $key = [Security.Cryptography.CngKey]::Import($blob, [Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
        $verifier = [Security.Cryptography.ECDsaCng]::new($key)
        try {
            $verifier.HashAlgorithm = [Security.Cryptography.CngAlgorithm]::Sha256
            # Manifest uses ASN.1 DER ECDSA; Windows CNG expects 32-byte r || s.
            $signature = [IO.File]::ReadAllBytes($signatureFile)
            if ($signature.Length -lt 8 -or $signature.Length -gt 72 -or $signature[0] -ne 48 -or $signature[1] -ne $signature.Length - 2) { throw 'Invalid release signature.' }
            $offset = 2
            $raw = New-Object byte[] 64
            foreach ($part in 0, 1) {
                if ($offset + 2 -gt $signature.Length -or $signature[$offset] -ne 2) { throw 'Invalid release signature.' }
                $length = [int]$signature[$offset + 1]
                $offset += 2
                if ($length -lt 1 -or $length -gt 33 -or $offset + $length -gt $signature.Length -or ($signature[$offset] -band 128)) { throw 'Invalid release signature.' }
                if ($length -eq 33) {
                    if ($signature[$offset] -ne 0) { throw 'Invalid release signature.' }
                    $offset++; $length--
                }
                [Array]::Copy($signature, $offset, $raw, ($part * 32 + 32 - $length), $length)
                $offset += $length
            }
            if ($offset -ne $signature.Length -or !$verifier.VerifyData([IO.File]::ReadAllBytes($manifestFile), $raw)) { throw 'Release signature verification failed. Nothing was installed.' }
        } finally { $verifier.Dispose(); $key.Dispose() }

        $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
        if ($manifest.product -cne 'Sweetmeter' -or $manifest.schema -ne 1 -or $manifest.version -cne $version -or $manifest.channel -cne 'stable') { throw 'Invalid release manifest.' }
        $asset = "Sweetmeter-$version-windows-x86_64.zip"
        $candidates = @($manifest.artifacts | Where-Object { $_.kind -ceq 'companion' -and $_.os -ceq 'windows' -and $_.arch -ceq 'x86_64' })
        if ($candidates.Count -ne 1) { throw 'Missing or ambiguous Windows package.' }
        $artifact = $candidates[0]
        if ($artifact.asset -cne $asset -or $artifact.version -cne $version -or $artifact.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $artifact.size -le 0 -or $artifact.size -gt 1073741824) { throw 'Invalid Windows package metadata.' }
        $package = Join-Path $temporary 'package.zip'
        Write-Host "Downloading Sweetmeter $version for Windows..."
        Invoke-WebRequest -UseBasicParsing -Uri "$release/$asset" -OutFile $package -TimeoutSec 600
        $sha = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($package)
        try {
            $digest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
            if ($digest -cne $artifact.sha256 -or $stream.Length -ne $artifact.size) { throw 'Package verification failed. Nothing was installed.' }
        } finally { $stream.Dispose(); $sha.Dispose() }
        # Verified against the signed manifest: drop any Mark-of-the-Web so the
        # extracted app is not treated as an unverified Internet download.
        Unblock-File -LiteralPath $package
        $extracted = Join-Path $temporary 'extracted'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($package, $extracted)
        $null = New-Item -ItemType Directory -Path $state -Force
        $null = New-Item -ItemType File -Path (Join-Path $state 'show-window') -Force
        Remove-Item -LiteralPath $errorFile, $resultFile -Force -ErrorAction SilentlyContinue
        # Installs, or replaces an installed copy that is older, damaged or
        # failing its self-test (atomically); a healthy current copy is kept.
        $process = Start-Process -FilePath (Join-Path $extracted 'Sweetmeter\Sweetmeter.exe') -ArgumentList '--install' -PassThru
        # Wait only for setup, not the long-running companion it starts.
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            $reason = if (Test-Path -LiteralPath $errorFile) { (Get-Content -LiteralPath $errorFile -Raw).Trim() } else { 'Sweetmeter setup did not finish.' }
            throw $reason
        }
        if (Test-Path -LiteralPath $resultFile) { Write-Host (Get-Content -LiteralPath $resultFile -Raw).Trim() }
        Write-Host 'Sweetmeter is opening. Allow Bluetooth if asked, then confirm this computer on the meter.'
    } catch {
        Write-Host ('Sweetmeter: ' + $_.Exception.Message) -ForegroundColor Red
        $global:LASTEXITCODE = 1
    } finally {
        if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
