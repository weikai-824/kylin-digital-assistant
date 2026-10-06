$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeArgs = @(
    "-p", "kylin-home",
    "-f", "docker-compose.yml",
    "-f", "docker-compose.dev.yml",
    "-f", "docker-compose.home.yml"
)

function Get-LocalJson([string]$Url) {
    $raw = & curl.exe --noproxy "*" --fail --silent --show-error $Url
    if ($LASTEXITCODE -ne 0) {
        throw "Request failed: $Url"
    }
    return $raw | ConvertFrom-Json
}

Push-Location $ProjectRoot
try {
    & docker compose @ComposeArgs ps
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the kylin-home stack"
    }

    $files = Get-LocalJson "http://127.0.0.1:7861/knowledge_base/list_files?knowledge_base_name=oa_ma"
    $esChildren = Get-LocalJson "http://127.0.0.1:9200/oa_ma_children/_count"
    $esParents = Get-LocalJson "http://127.0.0.1:9200/oa_ma_parents/_count"

    $milvusRaw = & curl.exe --noproxy "*" --fail --silent --show-error `
        -H "Content-Type: application/json" `
        -d '{"collectionName":"oa_ma_children"}' `
        "http://127.0.0.1:19530/v2/vectordb/collections/get_stats"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query Milvus"
    }
    $milvus = $milvusRaw | ConvertFrom-Json

    $result = [ordered]@{
        Api                  = "OK"
        KnowledgeFiles      = @($files.data).Count
        ElasticsearchChild  = [int]$esChildren.count
        ElasticsearchParent = [int]$esParents.count
        MilvusChild          = [int]$milvus.data.rowCount
        OllamaModels         = @(& docker exec kylin-home-ollama-1 ollama list | Select-Object -Skip 1)
    }

    if ($result.KnowledgeFiles -ne 12) { throw "Expected 12 knowledge files" }
    if ($result.ElasticsearchChild -ne 670) { throw "Expected 670 ES child chunks" }
    if ($result.ElasticsearchParent -ne 234) { throw "Expected 234 ES parent chunks" }
    if ($result.MilvusChild -ne 670) { throw "Expected 670 Milvus vectors" }
    if (-not ($result.OllamaModels -match "bge-m3")) { throw "bge-m3 is missing" }

    $result | Format-List
    Write-Host "Kylin home migration verification passed." -ForegroundColor Green
}
finally {
    Pop-Location
}
