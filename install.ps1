# Run in Windows PowerShell 5.1+ as the desktop user; no Python or Espressif app.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use install.sh on macOS/Linux.' }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($architecture -ne 'AMD64') { throw 'The Windows release currently requires x64.' }
    if ([Environment]::OSVersion.Version.Build -lt 22000) { throw 'Windows 11 or later is required.' }
    $state = Join-Path $env:LOCALAPPDATA 'Sweetmeter\state'
    $installed = Join-Path $env:LOCALAPPDATA 'Programs\Sweetmeter\Sweetmeter.exe'
    if (Test-Path -LiteralPath $installed) {
        $null = New-Item -ItemType Directory -Path $state -Force
        $null = New-Item -ItemType File -Path (Join-Path $state 'show-window') -Force
        Write-Host 'Opening your existing Sweetmeter. Use its updater for new versions.'
        Start-Process -FilePath $installed
        return
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('sweetmeter-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $temporary
    try {
        Write-Host 'Finding the latest Sweetmeter release...'
        $releaseInfo = Invoke-RestMethod -Uri 'https://api.github.com/repos/luvxinc/Sweetmeter/releases/latest' -TimeoutSec 60
        $version = $releaseInfo.tag_name
        if ($version -cnotmatch '^[0-9]{4}\.[1-9][0-9]?\.[1-9][0-9]*$' -or $releaseInfo.draft -or $releaseInfo.prerelease) { throw 'Unexpected release version.' }
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
        if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant() -cne $artifact.sha256 -or (Get-Item -LiteralPath $package).Length -ne $artifact.size) { throw 'Package verification failed. Nothing was installed.' }
        $extracted = Join-Path $temporary 'extracted'
        Expand-Archive -LiteralPath $package -DestinationPath $extracted
        $null = New-Item -ItemType Directory -Path $state -Force
        $null = New-Item -ItemType File -Path (Join-Path $state 'show-window') -Force
        if (Test-Path -LiteralPath $installed) {
            Write-Host 'Opening your existing Sweetmeter. Use its updater for new versions.'
            Start-Process -FilePath $installed
        } else {
            $process = Start-Process -FilePath (Join-Path $extracted 'Sweetmeter\Sweetmeter.exe') -ArgumentList '--install' -PassThru
            # Wait only for setup, not the long-running companion it starts.
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) { throw 'Sweetmeter installation failed.' }
        }
        Write-Host 'Sweetmeter is opening. Allow Bluetooth if asked, then confirm this computer on the meter.'
    } finally { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
