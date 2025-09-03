# Checklist Export (PGR)

Função Cloud Functions (Python) para gerar PDF de execução de checklist PGR.

## Estrutura
- `main.py`: entrypoint HTTP `main(request)`
- `requirements.txt`

## Variáveis de ambiente
- `GCS_BUCKET` (obrigatório em produção) — bucket GCS onde estão as imagens e o `logo.png`.
- `LOGO_BLOB` (opcional, default `logo.png`).
- `USE_GCS_FOR_STORAGE_URLS` (opcional, default `true`) — quando `true`, URLs do GCS são parseadas e baixadas via SDK (qualquer bucket com permissão da SA). Quando `false`, URLs são baixadas via HTTP (precisa ser pública ou Signed URL).
- `ALLOWED_IMAGE_HOSTS` (opcional) — hosts permitidos para download HTTP. Default: `storage.googleapis.com,storage.cloud.google.com`.
- `MAX_IMAGE_BYTES` (opcional) — tamanho máximo de download por imagem (default: `10485760`, 10MB).

## Pré-requisitos
- `gcloud` autenticado: `gcloud auth login`
- Projeto: `aiqia-backend-php-superapp`
- APIs habilitadas: Cloud Functions, Cloud Build, Artifact Registry, Cloud Run, Eventarc
- Service Account da função com acesso de leitura aos buckets usados (ex.: `roles/storage.objectViewer`)

## Deploy (Google Cloud Functions - 2ª Geração)
Execute os comandos dentro da pasta `docs/google functions/pgr-checklist-export/`.

```bash
# Configurar projeto e habilitar APIs
gcloud config set project aiqia-backend-php-superapp
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  eventarc.googleapis.com

# (Opcional) Garantir permissão de leitura nos buckets
gcloud projects add-iam-policy-binding aiqia-backend-php-superapp \
  --member="serviceAccount:aiqia-backend-php-superapp@appspot.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

# Deploy (Gen 2)
gcloud functions deploy pgr-checklist-export \
  --gen2 \
  --runtime=python311 \
  --region=southamerica-east1 \
  --project=aiqia-backend-php-superapp \
  --source=. \
  --entry-point=main \
  --trigger-http \
  --allow-unauthenticated \
  --set-env-vars "GCS_BUCKET=docs-superapp,USE_GCS_FOR_STORAGE_URLS=true,ALLOWED_IMAGE_HOSTS=storage.googleapis.com,storage.cloud.google.com,MAX_IMAGE_BYTES=10485760" \
  --service-account=aiqia-backend-php-superapp@appspot.gserviceaccount.com

# Obter URL
gcloud functions describe pgr-checklist-export --gen2 --region=southamerica-east1 --format="value(serviceConfig.uri)"
```

## Requisição
- Método: `POST`
- Querystring: `?key=<identificador>`
- Body (JSON) — payload normalizado para PGR:
```json
{
  "original": {
    "revisao": {
      "runDate": "2025-09-02 10:00:00",
      "placa": "ABC1234",
      "km": "12345",
      "tipo": "PGR",
      "descricao": "Observações PGR",
      "observacao_validacao": "OK",
      "name": "Colaborador X",
      "validador": "Validador Y",
      "data_validacao": "2025-09-02 12:00:00"
    },
    "itens": [
      {
        "item": "Verificar pontos de risco",
        "conforme": 1,
        "problema_identificado": "",
        "imagens": [ { "img_path": "caminho/no/bucket/arquivo.jpg" } ]
      }
    ]
  }
}
```

Observação: Se não for fornecido `conforme`, a função tentará mapear a partir de `situation` string (ex.: "OK" => 1).

## Resposta
- `application/pdf` com header `Content-Disposition: attachment; filename="checklist-pgr-<key>.pdf"`

## Observações de execução
- Quando `USE_GCS_FOR_STORAGE_URLS=true`, URLs como `gs://bucket/obj`, `https://storage.googleapis.com/bucket/obj` ou `https://bucket.storage.googleapis.com/obj` serão baixadas via SDK usando a SA da função.
- `img_path` continua suportado e busca no bucket definido em `GCS_BUCKET`.
- URLs não-GCS (ou quando `USE_GCS_FOR_STORAGE_URLS=false`) são baixadas via HTTP, respeitando `ALLOWED_IMAGE_HOSTS` e `MAX_IMAGE_BYTES`.
