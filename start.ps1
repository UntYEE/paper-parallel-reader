$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot

function Invoke-DockerProbe {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can promote harmless native stderr warnings to exceptions.
        $ErrorActionPreference = "Continue"
        $null = & docker @Arguments 2>&1
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "没有找到 Docker。请先安装 Docker Desktop：https://www.docker.com/products/docker-desktop/"
    }

    if ((Invoke-DockerProbe -Arguments @("compose", "version")) -ne 0) {
        throw "当前 Docker 没有 Compose 插件，请更新 Docker Desktop。"
    }

    if ((Invoke-DockerProbe -Arguments @("info")) -ne 0) {
        $dockerDesktop = Join-Path $Env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dockerDesktop) {
            Write-Host "正在启动 Docker Desktop..."
            Start-Process $dockerDesktop
            for ($attempt = 0; $attempt -lt 60; $attempt++) {
                Start-Sleep -Seconds 2
                if ((Invoke-DockerProbe -Arguments @("info")) -eq 0) {
                    break
                }
            }
        }
    }

    if ((Invoke-DockerProbe -Arguments @("info")) -ne 0) {
        throw "Docker Desktop 尚未就绪，请确认它已经完成启动。"
    }

    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
    }

    $envLines = [System.IO.File]::ReadAllLines((Join-Path $PWD ".env"))
    $keyLine = $envLines | Where-Object { $_ -match "^DEEPSEEK_API_KEY=" } | Select-Object -First 1
    $needsKey = (-not $keyLine) -or ($keyLine -match "^DEEPSEEK_API_KEY=\s*$") -or ($keyLine -match "^DEEPSEEK_API_KEY=填")
    if ($needsKey) {
        $secureKey = Read-Host "请输入你的 DeepSeek API Key（输入内容不会显示）" -AsSecureString
        $credential = New-Object System.Net.NetworkCredential("", $secureKey)
        $apiKey = $credential.Password
        if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey.Contains([Environment]::NewLine)) {
            throw "API Key 不能为空。"
        }

        $updated = $false
        for ($index = 0; $index -lt $envLines.Length; $index++) {
            if ($envLines[$index] -match "^DEEPSEEK_API_KEY=") {
                $envLines[$index] = "DEEPSEEK_API_KEY=$apiKey"
                $updated = $true
                break
            }
        }
        if (-not $updated) {
            $envLines += "DEEPSEEK_API_KEY=$apiKey"
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines((Join-Path $PWD ".env"), $envLines, $utf8)
    }

    Write-Host ""
    Write-Host "启动模式："
    Write-Host "  1. 普通版（推荐，启动更快）"
    Write-Host "  2. OCR 版（用于扫描 PDF，首次下载较大）"
    $mode = Read-Host "请选择 [1]"

    $composeArgs = @("compose", "-f", "compose.yaml")
    $ocrEnabled = $false
    if ($mode -eq "2") {
        $ocrEnabled = $true
        $composeArgs += @("-f", "compose.ocr.yaml")
    }

    Write-Host ""
    Write-Host "正在下载并启动 Paper Parallel Reader..."
    & docker @composeArgs pull
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        $buildLocally = Read-Host "预构建镜像下载失败，是否改为在本机编译？这会更慢。[y/N]"
        if ($buildLocally -notin @("y", "Y")) {
            throw "镜像下载失败，请检查网络后重试。"
        }
        if ($ocrEnabled) {
            $composeArgs += @("-f", "compose.build-ocr.yaml")
        }
        else {
            $composeArgs += @("-f", "compose.build.yaml")
        }
        & docker @composeArgs up -d --build
        if ($LASTEXITCODE -ne 0) {
            throw "本地镜像构建失败。"
        }
    }
    else {
        & docker @composeArgs up -d
        if ($LASTEXITCODE -ne 0) {
            throw "容器启动失败。"
        }
    }

    $portLine = $envLines | Where-Object { $_ -match "^APP_PORT=" } | Select-Object -Last 1
    $port = if ($portLine) { ($portLine -replace "^APP_PORT=", "").Trim() } else { "8000" }
    if ([string]::IsNullOrWhiteSpace($port)) {
        $port = "8000"
    }
    $url = "http://127.0.0.1:$port/viewer/"
    $healthUrl = "http://127.0.0.1:$port/api/health"

    Write-Host "正在等待服务就绪..."
    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $ready) {
        throw "服务启动超时。请运行 docker compose logs reader 查看原因。"
    }

    Write-Host "启动成功：$url"
    Start-Process $url
}
catch {
    Write-Host ""
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Read-Host "按回车键关闭窗口"
    exit 1
}
