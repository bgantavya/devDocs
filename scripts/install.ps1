# Installs the devdocs command line on Windows with uv (fetched with Astral's
# installer when missing), else pipx, else `pip install --user`; then runs
# `devdocs --version` and `devdocs doctor --offline` and prints a quick start.
# Running it again upgrades or reinstalls. Windows PowerShell 5.1 and PowerShell 7.
#
#   irm https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# A failure exits 1. Under iex, where exit would close the session, it sets
# $LASTEXITCODE to 1 and throws, so `powershell -c "irm ... | iex"` still exits 1.
# Settings, as environment variables:
#   DEVDOCS_VERSION         release to install, e.g. 4.0.0 (default: the latest)
#   DEVDOCS_SOURCE          local path or URL to install from instead of PyPI
#   DEVDOCS_PYTHON_VERSION  Python for devdocs, 3.11 or newer (default: 3.13)
#   DEVDOCS_NO_UV_INSTALL   1: never install uv; without uv, use pipx, then pip

& {
    Set-StrictMode -Version 3.0
    $ErrorActionPreference = 'Stop'

    $Tag = 'devdocs-install:'
    $UvInstaller = 'https://astral.sh/uv/install.ps1'

    function Write-Step([string] $Message) {
        Write-Host "$Tag $Message"
    }

    function Write-Problem([string] $Message) {
        [Console]::Error.WriteLine("$Tag warning: $Message")
    }

    function Invoke-Native {
        <# Runs a program with its output on the console and returns its exit code.
           stderr carries progress, so only the exit code decides success. #>
        param([string] $FilePath, [string[]] $ArgumentList)
        $ErrorActionPreference = 'Continue'
        try {
            & $FilePath @ArgumentList | Out-Host
            return $LASTEXITCODE
        } catch {
            Write-Problem "$FilePath did not start: $($_.Exception.Message)"
            return 127
        }
    }

    function Invoke-Capture {
        <# Runs a program and returns @{ Code; Output }: its stdout lines, plus its
           stderr lines with -MergeErrors; stderr is dropped otherwise. #>
        param([string] $FilePath, [string[]] $ArgumentList, [switch] $MergeErrors)
        $ErrorActionPreference = 'Continue'
        try {
            if ($MergeErrors) {
                $output = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" })
            } else {
                $output = @(& $FilePath @ArgumentList 2>$null)
            }
            return @{ Code = $LASTEXITCODE; Output = $output }
        } catch {
            return @{ Code = 127; Output = @("$($_.Exception.Message)") }
        }
    }

    function Get-FirstLine([object[]] $Lines) {
        if (-not $Lines) { return '' }
        return ([string] $Lines[0]).Trim()
    }

    function Test-SameDir([string] $First, [string] $Second) {
        try {
            $a = [IO.Path]::GetFullPath($First.Trim('"')).TrimEnd([char[]] '\/')
            $b = [IO.Path]::GetFullPath($Second.Trim('"')).TrimEnd([char[]] '\/')
            return ($a -ieq $b)
        } catch {
            return $false
        }
    }

    function Resolve-Source {
        <# The requirement handed to the installer, as @{ Spec; Origin }, where
           Origin is its display form with any URL credentials hidden. #>
        if ($env:DEVDOCS_SOURCE) {
            if ($env:DEVDOCS_VERSION) {
                Write-Problem 'DEVDOCS_VERSION is ignored because DEVDOCS_SOURCE is set'
            }
            $source = $env:DEVDOCS_SOURCE
            if ($source -match '://') {
                return @{ Spec = $source; Origin = ($source -replace '(://)[^/@]*@', '$1***@') }
            }
            if (-not (Test-Path -LiteralPath $source)) {
                throw "DEVDOCS_SOURCE is neither a URL nor an existing path: $source"
            }
            $full = (Resolve-Path -LiteralPath $source).ProviderPath.TrimEnd('\')
            if ($full -match '^[A-Za-z]:$') { $full += '\' }   # a drive root keeps its slash
            return @{ Spec = $full; Origin = $full }
        }
        if ($env:DEVDOCS_VERSION) {
            $release = $env:DEVDOCS_VERSION -replace '^v', ''
            if ($release -notmatch '^[0-9A-Za-z.!+-]+$') {
                throw "DEVDOCS_VERSION is not a version: $($env:DEVDOCS_VERSION) (e.g. 4.0.0)"
            }
            return @{ Spec = "devdocs==$release"; Origin = "PyPI (version $release)" }
        }
        return @{ Spec = 'devdocs >= 4'; Origin = 'PyPI (latest release)' }   # spaced, so quoted: no redirect in a .bat shim
    }

    function Find-Uv {
        <# A usable uv.exe: the one on Path, else one where Astral's installer puts it. #>
        $command = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($command) { return $command.Path }
        $dirs = @()
        if ($env:UV_INSTALL_DIR) {
            $dirs += $env:UV_INSTALL_DIR
            $dirs += Join-Path -Path $env:UV_INSTALL_DIR -ChildPath 'bin'
        }
        if ($env:XDG_BIN_HOME) { $dirs += $env:XDG_BIN_HOME }
        if ($env:XDG_DATA_HOME) { $dirs += Join-Path -Path $env:XDG_DATA_HOME -ChildPath '..\bin' }
        $dirs += Join-Path -Path $HOME -ChildPath '.local\bin'
        $dirs += Join-Path -Path $HOME -ChildPath '.cargo\bin'
        foreach ($dir in $dirs) {
            $candidate = Join-Path -Path $dir -ChildPath 'uv.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
        return $null
    }

    function Find-Python([string] $PythonVersion) {
        <# The first Python 3.11+ outside a virtual environment, as a full path: the
           py launcher (DEVDOCS_PYTHON_VERSION when set, then the newest 3.x) first,
           then python.exe and python3.exe on Path. #>
        $probe = 'import sys; print(sys.executable) if sys.version_info >= (3, 11) and sys.prefix == sys.base_prefix else sys.exit(1)'
        $candidates = @()
        $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($launcher) {
            if ($env:DEVDOCS_PYTHON_VERSION) {
                $candidates += @{ Exe = $launcher.Path; Flags = @("-$PythonVersion") }
            }
            $candidates += @{ Exe = $launcher.Path; Flags = @('-3') }
        }
        $onPath = @(Get-Command python, python3 -CommandType Application -All -ErrorAction SilentlyContinue)
        foreach ($command in $onPath) {
            $candidates += @{ Exe = $command.Path; Flags = @() }
        }
        foreach ($candidate in $candidates) {
            $result = Invoke-Capture $candidate.Exe ($candidate.Flags + @('-I', '-c', $probe))
            $path = Get-FirstLine $result.Output
            if ($result.Code -eq 0 -and $path) { return $path }
        }
        return $null
    }

    function Install-Uv {
        <# Runs Astral's installer in a child PowerShell, whose `exit` cannot close
           this session; returns whether it succeeded. #>
        Write-Step "installing uv with Astral's installer ($UvInstaller)"
        $shell = (Get-Process -Id $PID).Path
        if (-not $shell -or (Split-Path -Path $shell -Leaf) -notmatch '^(pwsh|powershell)\.exe$') {
            $shell = 'powershell.exe'
        }
        $command = '[Net.ServicePointManager]::SecurityProtocol = ' +
            '[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; ' +
            "irm $UvInstaller | iex"
        $code = Invoke-Native $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command)
        return ($code -eq 0)
    }

    function Install-WithUv([string] $Uv, [string] $PythonVersion, [hashtable] $Source, [bool] $Fresh) {
        $version = Invoke-Capture $Uv @('--version')
        if ($version.Code -ne 0) { throw "$Uv does not run" }
        $uvVersion = (Get-FirstLine $version.Output) -replace ' \(.*$', ''
        $binDir = Get-FirstLine (Invoke-Capture $Uv @('tool', 'dir', '--bin')).Output
        if (-not $binDir) {
            throw "$uvVersion at $Uv is too old for 'uv tool'; update it (uv self update) and run this again"
        }
        Write-Step "installing devdocs from $($Source.Origin) with $uvVersion and Python $PythonVersion"
        $code = Invoke-Native $Uv @('tool', 'install', '--python', $PythonVersion, '--force',
            '--reinstall', '--upgrade', $Source.Spec)
        if ($code -ne 0) { throw 'uv could not install devdocs (see above)' }

        $pathEdited = -not ($env:UV_NO_MODIFY_PATH -or $env:INSTALLER_NO_MODIFY_PATH)
        if ($Fresh -and $pathEdited -and (Test-SameDir (Split-Path -Path $Uv -Parent) $binDir)) {
            $hint = "open a new terminal (uv's installer added it to your user Path)"
        } else {
            $onPath = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($onPath -and $onPath.Path -ieq $Uv) {
                $uvWord = 'uv'
            } elseif ($Uv -match ' ') {
                $uvWord = "& '$Uv'"
            } else {
                $uvWord = $Uv
            }
            $hint = "run $uvWord tool update-shell, then open a new terminal"
        }
        return @{ BinDir = $binDir; Hint = $hint }
    }

    function Install-WithPipx([string] $Pipx, [hashtable] $Source, [string] $PythonVersion) {
        $python = Find-Python $PythonVersion
        if (-not $python) {
            throw 'no Python 3.11 or newer found for pipx to install devdocs with; install Python 3.11+ or uv (https://docs.astral.sh/uv/), then run this again'
        }
        if ($Source.Origin -match '://\*\*\*@') {
            throw 'pipx can print the credentials in DEVDOCS_SOURCE; use a URL without them (pip reads a netrc file), or install uv (https://docs.astral.sh/uv/) and run this again'
        }
        $venvs = Get-FirstLine (Invoke-Capture $Pipx @('environment', '--value', 'PIPX_LOCAL_VENVS')).Output
        if (-not $venvs) {
            $pipxHome = if ($env:PIPX_HOME) { $env:PIPX_HOME } else { Join-Path -Path $HOME -ChildPath '.local\pipx' }
            $venvs = Join-Path -Path $pipxHome -ChildPath 'venvs'
        }
        $venv = Join-Path -Path $venvs -ChildPath 'devdocs'
        if (Test-Path -LiteralPath $venv -PathType Container) {
            $venvPython = Join-Path -Path $venv -ChildPath 'Scripts\python.exe'
            $current = Invoke-Capture $venvPython @('-I', '-c', 'import sys; sys.exit(sys.version_info < (3, 11))')
            if ($current.Code -ne 0) {
                Push-Location -LiteralPath "$env:SystemDrive\"   # old pipx takes .\devdocs for a path
                try {
                    $removed = Invoke-Capture $Pipx @('uninstall', 'devdocs')   # --force keeps its Python
                } finally {
                    Pop-Location
                }
                if ($removed.Code -eq 0) {
                    Write-Step 'removed the previous pipx install of devdocs: its Python is missing or older than 3.11'
                }
            }
        }
        Write-Step "installing devdocs from $($Source.Origin) with pipx and $python"
        $code = Invoke-Native $Pipx @('install', '--force', '--python', $python, $Source.Spec)
        if ($code -ne 0) { throw 'pipx could not install devdocs (see above)' }
        $binDir = Get-FirstLine (Invoke-Capture $Pipx @('environment', '--value', 'PIPX_BIN_DIR')).Output
        if (-not $binDir) {
            $binDir = if ($env:PIPX_BIN_DIR) { $env:PIPX_BIN_DIR } else { Join-Path -Path $HOME -ChildPath '.local\bin' }
        }
        return @{ BinDir = $binDir; Hint = 'run pipx ensurepath, then open a new terminal' }
    }

    function Install-WithPip([hashtable] $Source, [string] $PythonVersion) {
        $python = Find-Python $PythonVersion
        if (-not $python) {
            if ($env:DEVDOCS_NO_UV_INSTALL -eq '1') {
                throw 'no uv, no pipx and no Python 3.11 or newer found; install Python 3.11+, or unset DEVDOCS_NO_UV_INSTALL to let this script install uv'
            }
            throw 'no uv, no pipx and no Python 3.11 or newer found; install uv (https://docs.astral.sh/uv/) or Python 3.11+, then run this again'
        }
        if ((Invoke-Capture $python @('-I', '-m', 'pip', '--version')).Code -ne 0) {
            throw "$python has no pip; install pipx or uv and run this again"
        }
        Write-Step "installing devdocs from $($Source.Origin) with pip --user and $python"
        $code = Invoke-Native $python @('-P', '-m', 'pip', 'install', '--user', '--upgrade',
            '--force-reinstall', '--no-warn-script-location', '--disable-pip-version-check', $Source.Spec)
        if ($code -ne 0) { throw 'pip could not install devdocs (see above)' }
        $where = 'import sysconfig; print(sysconfig.get_path(''scripts'', sysconfig.get_preferred_scheme(''user'')))'
        $binDir = Get-FirstLine (Invoke-Capture $python @('-I', '-c', $where)).Output
        if (-not $binDir) { throw "$python did not report where pip puts commands" }
        return @{ BinDir = $binDir; Hint = 'add it to your user Path environment variable, then open a new terminal' }
    }

    function Test-Install([string] $BinDir) {
        $exe = Join-Path -Path $BinDir -ChildPath 'devdocs.exe'
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            throw "the install finished, but $exe is missing"
        }
        $version = Invoke-Capture $exe @('--version') -MergeErrors
        if ($version.Code -ne 0) { throw "$exe does not run: $($version.Output -join ' ')" }
        Write-Step "installed $(Get-FirstLine $version.Output) at $exe"
        Write-Step "checking the setup with 'devdocs doctor --offline'"
        $doctor = Invoke-Capture $exe @('doctor', '--offline') -MergeErrors
        foreach ($line in $doctor.Output) {
            if ($line) { Write-Host "    $line" } else { Write-Host '' }
        }
        switch ($doctor.Code) {
            0 { Write-Step 'doctor found nothing that blocks a run' }
            1 { Write-Step 'doctor reports something to fix before a run (lines marked !!), usually the API key; see the quick start' }
            default { Write-Problem "doctor exited with status $($doctor.Code); devdocs is installed, but fix what it reports" }
        }
    }

    function Show-PathAdvice([string] $BinDir, [string] $Hint) {
        $found = Get-Command devdocs -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found -and (Test-SameDir (Split-Path -Path $found.Path -Parent) $BinDir)) { return }
        if ($found) {
            Write-Problem "'devdocs' on Path runs $($found.Path), not the one just installed; remove that one (for v3: pip uninstall devDocs) or put $BinDir first on Path"
        }
        $entries = @($env:Path -split ';' | Where-Object { $_ -and (Test-SameDir $_ $BinDir) })
        if ($entries.Count -eq 0) {
            Write-Step "$BinDir is not on Path yet: $Hint"
            Write-Step ('for this terminal only: $env:Path = "' + $BinDir + ';$env:Path"')
        }
    }

    function Show-QuickStart {
        Write-Host @'

Quick start
  1. Get an API key:
       Gemini  https://aistudio.google.com/apikey
       Claude  https://console.anthropic.com/
  2. Set it for this terminal (setx GEMINI_API_KEY your-key keeps it for new ones):
       $env:GEMINI_API_KEY = 'your-key'
       $env:ANTHROPIC_API_KEY = 'your-key'; $env:DEVDOCS_PROVIDER = 'anthropic'    # for Claude
  3. Document a project:
       cd your-project
       devdocs estimate    # free: prices the run, sends nothing
       devdocs generate    # writes the docs to docs\
'@
    }

    function Install-DevDocs {
        if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
            throw 'install.ps1 is for Windows; on macOS and Linux run: curl -LsSf https://raw.githubusercontent.com/bgantavya/devDocs/v4/scripts/install.sh | sh'
        }
        if ($PSVersionTable.PSVersion.Major -lt 5) {
            throw 'this installer needs Windows PowerShell 5.1 or PowerShell 7'
        }
        $pythonVersion = if ($env:DEVDOCS_PYTHON_VERSION) { $env:DEVDOCS_PYTHON_VERSION } else { '3.13' }
        if ($pythonVersion -match '^([0-2](\..*)?|3\.([0-9]|10)(\..*)?)$') {
            throw "DEVDOCS_PYTHON_VERSION=${pythonVersion}: devdocs needs Python 3.11 or newer"
        }
        $source = Resolve-Source

        $uv = Find-Uv
        $fresh = $false
        if (-not $uv) {
            if ($env:DEVDOCS_NO_UV_INSTALL -eq '1') {
                Write-Step 'uv is not installed and DEVDOCS_NO_UV_INSTALL=1; using pipx or pip'
            } else {
                if (Install-Uv) { $uv = Find-Uv }
                if ($uv) { $fresh = $true } else { Write-Problem 'could not install uv; trying pipx, then pip' }
            }
        }

        $pipx = Get-Command pipx -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($uv) {
            $result = Install-WithUv $uv $pythonVersion $source $fresh
        } elseif ($pipx) {
            $result = Install-WithPipx $pipx.Path $source $pythonVersion
        } else {
            $result = Install-WithPip $source $pythonVersion
        }
        Test-Install $result.BinDir
        Show-PathAdvice $result.BinDir $result.Hint
        Show-QuickStart
    }

    try {
        Install-DevDocs
        $global:LASTEXITCODE = 0   # doctor's 1 (no key yet) is not a failure
    } catch {
        $message = "$Tag error: $($_.Exception.Message)"
        if (Get-Variable -Name PSCommandPath -ValueOnly -ErrorAction SilentlyContinue) {
            [Console]::Error.WriteLine($message)
            exit 1   # a script run: fail through the exit code
        }
        $global:LASTEXITCODE = 1
        throw $message   # irm | iex: exit would close the session; a throw fails powershell -c
    }
}
