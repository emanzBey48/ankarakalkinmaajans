# demo-site statik sunumunu Vercel production'a gonderir.
#
#   .\deploy-vercel.ps1
#
# Token bu dosyada degil, Vercel CLI'inin kendi deposunda duruyor:
#   %APPDATA%\com.vercel.cli\Data\auth.json
# (vercel login ile ayni yer). Proje baglantisi demo-site\.vercel icinde,
# yani tekrar calistirinca ayni "demo-site" projesine gider.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$site = Join-Path $root 'demo-site'

if (-not (Get-Command vercel.cmd -ErrorAction SilentlyContinue)) {
    Write-Error 'vercel CLI bulunamadi. Kurmak icin: npm.cmd i -g vercel'
}

# --- TLS interceptor onlemi ---------------------------------------------------
# Bu agda TLS araya giriyor (issuer: CN=MEB-CERT-TTVPN). Node kendi CA
# listesini kullandigi icin MEB kok CA'sini tanimiyor ve vercel CLI
# "ECONNRESET" / "ETIMEDOUT" ile duserdi. PowerShell etkilenmiyor cunku
# Windows sertifika deposunu kullanir.
# Cozum: kok CA'yi PEM'e aktarip NODE_EXTRA_CA_CERTS ile tanitmak.
$thumbprint = '3CC057FF39FB8599995035ED4C36D05C6E620ADF' # CN=fatihca, DC=mebca, DC=gov, DC=tr
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store('Root', 'CurrentUser')
try {
    $store.Open('ReadOnly')
    $ca = $store.Certificates | Where-Object { $_.Thumbprint -eq $thumbprint } | Select-Object -First 1
} finally {
    $store.Close()
}

if ($ca) {
    $pem = Join-Path $env:TEMP 'mebca-root.pem'
    $body = [Convert]::ToBase64String($ca.Export('Cert'), 'InsertLineBreaks')
    [System.IO.File]::WriteAllText($pem, "-----BEGIN CERTIFICATE-----`r`n$body`r`n-----END CERTIFICATE-----")
    $env:NODE_EXTRA_CA_CERTS = $pem
} else {
    Write-Warning 'MEB kok CA bulunamadi; interceptor yoksa gerek de yok.'
}

$env:VERCEL_TELEMETRY_DISABLED = '1'

& vercel.cmd deploy --prod --yes --cwd $site
exit $LASTEXITCODE
