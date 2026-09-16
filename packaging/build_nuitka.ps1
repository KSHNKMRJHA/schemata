# Compile the packaged Schemata app with Nuitka (standalone/dist folder).
# Produces dist\Schemata.dist\Schemata.exe relative to the repo root.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'

if (Test-Path "$root\dist\Schemata.dist") { Remove-Item "$root\dist\Schemata.dist" -Recurse -Force }
if (Test-Path "$root\dist\Schemata.build") { Remove-Item "$root\dist\Schemata.build" -Recurse -Force }

& $py -m nuitka `
  --standalone `
  --assume-yes-for-downloads `
  --windows-console-mode=disable `
  "--windows-icon-from-ico=$root\packaging\icon.ico" `
  --windows-product-name=Schemata `
  "--windows-company-name=Kishan J." `
  --windows-product-version=2.4.0 `
  --windows-file-version=2.4.0.0 `
  "--windows-file-description=Schemata - Component Intelligence Platform" `
  --product-name=Schemata `
  --product-version=2.4.0 `
  --output-filename=Schemata.exe `
  --mingw64 `
  --lto=yes `
  --jobs=16 `
  --enable-plugin=tk-inter `
  --enable-plugin=anti-bloat `
  --noinclude-pytest-mode=nofollow `
  --noinclude-setuptools-mode=nofollow `
  --nofollow-import-to=unittest,test,pytest,_pytest,doctest,pdb,pdbpp,setuptools,pip,distutils,pkg_resources `
  --include-package=app `
  --include-module=openpyxl `
  --include-module=python_multipart `
  "--include-data-dir=$root\ui=ui" `
  "--include-data-file=$root\config.toml=config.toml" `
  "--include-data-file=$root\packaging\icon.ico=icon.ico" `
  "--output-dir=$root\dist" `
  "$root\packaging\launcher.py"

if ($LASTEXITCODE -ne 0) { throw "Nuitka failed with exit code $LASTEXITCODE" }
# Nuitka names the output folder after the entry module (launcher), but the
# installer expects dist\Schemata.dist — rename it so ISCC picks the right tree.
if (Test-Path "$root\dist\Schemata.dist") { Remove-Item "$root\dist\Schemata.dist" -Recurse -Force }
if (Test-Path "$root\dist\launcher.dist") {
  Move-Item -LiteralPath "$root\dist\launcher.dist" -Destination "$root\dist\Schemata.dist"
}
if (Test-Path "$root\dist\launcher.build") { Remove-Item "$root\dist\launcher.build" -Recurse -Force }

# Guard: never ship credential files (.env / private keys). CA cert bundles in
# site-packages (e.g. certifi\cacert.pem) are public TLS roots and are fine.
$leaked = Get-ChildItem "$root\dist\Schemata.dist" -Recurse -Force -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq '.env' -or $_.Name -like '.env.*' -or $_.Extension -eq '.key' -or $_.Name -like 'id_rsa*' -or $_.Name -like 'id_ed25519*'
}
if ($leaked) { throw "CREDENTIAL FILES BUNDLED IN BUILD: $($leaked.FullName -join ', ') - remove before publishing" }
Write-Host "Build OK. Dist at: $root\dist\Schemata.dist"