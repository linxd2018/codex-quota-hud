$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$frameworkRoot = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$metadataRoot = Join-Path $env:WINDIR 'System32\WinMetadata'
$vectorsDll = Get-ChildItem (Join-Path $env:WINDIR 'Microsoft.NET\assembly\GAC_MSIL\System.Numerics.Vectors') -Recurse -Filter System.Numerics.Vectors.dll | Select-Object -First 1 -ExpandProperty FullName
if (-not $vectorsDll) { throw 'System.Numerics.Vectors system assembly is unavailable' }
$references = @(
    (Join-Path $frameworkRoot 'System.Runtime.dll'),
    (Join-Path $frameworkRoot 'System.Runtime.WindowsRuntime.dll'),
    (Join-Path $frameworkRoot 'System.Numerics.dll'),
    (Join-Path $metadataRoot 'Windows.UI.winmd'),
    (Join-Path $metadataRoot 'Windows.Foundation.winmd'),
    (Join-Path $metadataRoot 'Windows.System.winmd'),
    $vectorsDll
)
$compilerArgs = @('/nologo', '/target:library', '/platform:x64', '/optimize+', ('/out:' + (Join-Path $projectRoot 'native\RoundedBackdrop.dll')))
foreach ($reference in $references) { $compilerArgs += ('/reference:' + $reference) }
$compilerArgs += (Join-Path $projectRoot 'native\RoundedBackdrop.cs')
& (Join-Path $frameworkRoot 'csc.exe') @compilerArgs
if ($LASTEXITCODE -ne 0) { throw "C# compiler failed: $LASTEXITCODE" }
Get-FileHash (Join-Path $projectRoot 'native\RoundedBackdrop.dll') -Algorithm SHA256
