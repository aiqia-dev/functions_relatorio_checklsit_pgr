import os
import io
from datetime import datetime
from typing import Dict, Optional, List, Tuple

from google.cloud import storage
from PIL import Image
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from concurrent.futures import ThreadPoolExecutor
import traceback
from flask import Request, Response
from dotenv import load_dotenv
import requests
from urllib.parse import urlparse, unquote


class PgrChecklistPDFGenerator:
    def __init__(self):
        # Carrega variáveis do .env quando executado localmente
        load_dotenv()
        self.gcs_client = storage.Client()
        self.bucket_name = os.getenv('GCS_BUCKET', 'docs-superapp')
        self.bucket = self.gcs_client.bucket(self.bucket_name)

        self.logo_blob = os.getenv('LOGO_BLOB', 'logo.png')

        # Limites e segurança para download via URL
        self.max_image_bytes = int(os.getenv('MAX_IMAGE_BYTES', '10485760'))  # 10MB
        self.allowed_image_hosts = set(
            h.strip() for h in os.getenv('ALLOWED_IMAGE_HOSTS', 'storage.googleapis.com,storage.cloud.google.com').split(',') if h.strip()
        )
        # Controle de preferência: usar SDK do GCS ao encontrar URLs do storage
        self.use_gcs_for_storage_urls = os.getenv('USE_GCS_FOR_STORAGE_URLS', 'true').strip().lower() in ('1', 'true', 'yes')

        # Configurações do PDF / imagens
        self.img_width = 55
        self.img_height = 35
        self.img_margin = 5
        self.line_height = 6

    def quick_reencode_jpg(self, image_data: bytes, quality: int = 25) -> Optional[bytes]:
        try:
            with Image.open(io.BytesIO(image_data)) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='JPEG', quality=quality, optimize=True)
                return output_buffer.getvalue()
        except Exception as e:
            print(f"Alerta: Falha ao re-codificar imagem. Ignorada. Erro: {str(e)}")
            return None

    def download_images_batch(self, image_paths: List[str]) -> List[Optional[bytes]]:
        try:
            batch_size = 100
            results = [None] * len(image_paths)

            for chunk_start in range(0, len(image_paths), batch_size):
                chunk_paths = image_paths[chunk_start:chunk_start + batch_size]
                batch = self.gcs_client.batch()
                blobs = [self.bucket.blob(path) for path in chunk_paths]
                with batch:
                    for i, blob in enumerate(blobs):
                        global_idx = chunk_start + i
                        if blob.exists():
                            try:
                                results[global_idx] = blob.download_as_bytes()
                            except Exception as e:
                                print(f"Alerta: Erro ao baixar {blob.name}: {str(e)}")
                                results[global_idx] = None
                        else:
                            print(f"Alerta: Imagem não encontrada no GCS: {blob.name}")
                            results[global_idx] = None
            return results
        except Exception as e:
            print(f"Erro ao baixar imagens em lote: {str(e)}")
            return [None] * len(image_paths)

    def _parse_gcs_url(self, url: str) -> Optional[Tuple[str, str]]:
        try:
            if not self.use_gcs_for_storage_urls:
                return None
            parsed = urlparse(url)
            host = parsed.netloc
            path = parsed.path
            # gs://bucket/obj
            if parsed.scheme == 'gs':
                parts = path.lstrip('/').split('/', 1)
                if len(parts) != 2:
                    return None
                return (parsed.netloc, unquote(parts[1]))
            # https://storage.googleapis.com/bucket/obj
            if host == 'storage.googleapis.com' or host == 'storage.cloud.google.com':
                parts = path.lstrip('/').split('/', 1)
                if len(parts) != 2:
                    return None
                bucket = parts[0]
                object_path = parts[1]
                return (bucket, unquote(object_path))
            # https://bucket.storage.googleapis.com/obj (virtual hosted style)
            if host.endswith('.storage.googleapis.com'):
                bucket = host.split('.storage.googleapis.com')[0]
                object_path = path.lstrip('/')
                if not object_path:
                    return None
                return (bucket, unquote(object_path))
            return None
        except Exception:
            return None

    def download_gcs_targets_batch(self, targets: List[Tuple[str, str]]) -> List[Optional[bytes]]:
        """Baixa objetos GCS especificando (bucket, object_path) por item."""
        try:
            results: List[Optional[bytes]] = [None] * len(targets)
            if not targets:
                return results
            # Agrupar por bucket para minimizar overhead
            by_bucket: Dict[str, List[Tuple[int, str]]] = {}
            for idx, (bucket_name, object_path) in enumerate(targets):
                by_bucket.setdefault(bucket_name, []).append((idx, object_path))

            for bucket_name, items in by_bucket.items():
                bucket = self.gcs_client.bucket(bucket_name)
                for idx, object_path in items:
                    blob = bucket.blob(object_path)
                    try:
                        if blob.exists():
                            results[idx] = blob.download_as_bytes()
                        else:
                            print(f"Alerta: Imagem não encontrada no GCS: gs://{bucket_name}/{object_path}")
                            results[idx] = None
                    except Exception as e:
                        print(f"Alerta: Erro ao baixar gs://{bucket_name}/{object_path}: {str(e)}")
                        results[idx] = None
            return results
        except Exception as e:
            print(f"Erro ao baixar imagens GCS (bucket,obj) em lote: {str(e)}")
            return [None] * len(targets)

    def _download_single_url(self, url: str) -> Optional[bytes]:
        try:
            parsed = urlparse(url)
            if parsed.hostname and not any(
                parsed.hostname == h or parsed.hostname.endswith('.' + h) for h in self.allowed_image_hosts
            ):
                print(f"Host não permitido para download: {parsed.hostname}")
                return None
            with requests.get(url, stream=True, timeout=20) as r:
                r.raise_for_status()
                total = 0
                buf = io.BytesIO()
                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_image_bytes:
                        print("Imagem excede tamanho máximo permitido")
                        return None
                    buf.write(chunk)
                data = buf.getvalue()
                # Valida abrindo com Pillow (alguns servidores retornam application/octet-stream)
                try:
                    with Image.open(io.BytesIO(data)) as _:
                        pass
                except Exception as e:
                    print(f"Download não parece ser imagem válida: {str(e)}")
                    return None
                return data
        except Exception as e:
            print(f"Erro ao baixar URL {url}: {str(e)}")
            return None

    def download_urls_batch(self, image_urls: List[str]) -> List[Optional[bytes]]:
        results: List[Optional[bytes]] = [None] * len(image_urls)
        if not image_urls:
            return results
        with ThreadPoolExecutor() as executor:
            future_to_idx = {executor.submit(self._download_single_url, url): i for i, url in enumerate(image_urls)}
            for future in future_to_idx:
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    print(f"Erro no download paralelo de URL índice {idx}: {str(e)}")
                    results[idx] = None
        return results

    def _get_text_line_count(self, pdf: FPDF, text: str, width: float) -> int:
        if not text:
            return 1
        lines = text.split('\n')
        total_lines = 0
        for line in lines:
            if not line:
                total_lines += 1
                continue
            words = line.split(' ')
            current_line = ""
            line_count_for_segment = 1
            for word in words:
                if pdf.get_string_width(word) > width:
                    if current_line:
                        line_count_for_segment += 1
                    current_line = ""
                    continue
                test_line = current_line + " " + word if current_line else word
                if pdf.get_string_width(test_line) <= width:
                    current_line = test_line
                else:
                    line_count_for_segment += 1
                    current_line = word
            total_lines += line_count_for_segment
        return total_lines

    def _format_date(self, date_str: Optional[str]) -> str:
        if not date_str:
            return 'N/A'
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ'):
            try:
                return datetime.strptime(date_str, fmt).strftime('%d-%m-%Y %H:%M:%S')
            except Exception:
                continue
        return 'Data inválida'

    def generate_pdf(self, request_data: Dict, key: str) -> bytes:
        try:
            original = request_data.get('original', {})
            if not isinstance(original, dict):
                raise ValueError("'original' deve ser um objeto")

            items = original.get('itens', [])
            revisao = original.get('revisao', {})

            # Regras PGR: situation é string. Idealmente vem pré-mapeado no payload
            # como conforme: 1/0. Se não, tentamos mapear:
            def to_conforme(v):
                if isinstance(v, int):
                    return v
                if isinstance(v, str):
                    vs = v.strip().lower()
                    return 1 if vs in ('ok', 'conforme', 'aprovado', 'positivo') else 0
                if isinstance(v, bool):
                    return 1 if v else 0
                return 0

            for it in items:
                if 'conforme' not in it and 'situation' in it:
                    it['conforme'] = to_conforme(it.get('situation'))

            ok_items = [item for item in items if item.get('conforme') == 1]
            nok_items = [item for item in items if item.get('conforme') == 0]

            complemento_checklist = {
                "perguntas": {"ok": len(ok_items), "nok": len(nok_items)},
                "totalFotos": {
                    "ok": sum(len(item.get('imagens', [])) for item in ok_items),
                    "nok": sum(len(item.get('imagens', [])) for item in nok_items),
                },
            }

            run_date = self._format_date(revisao.get('runDate') or revisao.get('data_revisao'))
            data_validacao = self._format_date(revisao.get('data_validacao'))

            placa = (revisao.get('placa') or 'N/A')
            km = (revisao.get('km') or 'N/A')
            tipo_evento = (revisao.get('tipo_evento') or revisao.get('tipo') or 'PGR')
            descricao = (revisao.get('descricao') or 'N/A').strip()
            observacao_validacao = (revisao.get('observacao_validacao') or 'N/A').strip()
            colaborador = (revisao.get('name') or 'N/A')
            validador = (revisao.get('validador') or 'N/A')

            pdf = FPDF()
            pdf.set_title(f'Checklist PGR - {key}')
            pdf.set_margins(10, 10, 10)
            pdf.set_auto_page_break(True, margin=15)
            pdf.add_page()
            pdf.set_font("helvetica", size=10)

            try:
                blob = self.bucket.blob(self.logo_blob)
                if blob.exists():
                    logo_bytes = blob.download_as_bytes()
                    with io.BytesIO(logo_bytes) as logo_buffer:
                        pdf.image(logo_buffer, x=10, y=12, w=25)
                else:
                    pdf.text(10, 15, 'Logo N/A')
            except Exception as e:
                pdf.text(10, 15, 'Logo N/A')
                print(f"Erro ao carregar logo do bucket: {str(e)}")

            pdf.set_y(10)
            pdf.set_x(40)

            pdf.cell(50, self.line_height, f"Código: {key}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(50, self.line_height, f"KM: {km}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(0, self.line_height, f"Data Execução: {run_date}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.set_x(40)
            pdf.cell(50, self.line_height, f"Placa: {placa}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(50, self.line_height, f"Tipo: {tipo_evento}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(0, self.line_height, f"Data Validação: {data_validacao}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(5)

            page_width = pdf.w - pdf.l_margin - pdf.r_margin
            gap = 2
            col_width = (page_width - gap) / 2

            start_y = pdf.get_y()
            pdf.multi_cell(col_width, self.line_height - 1, f"Descrição:\n{descricao or 'Nenhuma'}", border=1)
            y_after_left = pdf.get_y()

            pdf.set_xy(pdf.l_margin + col_width + gap, start_y)
            pdf.multi_cell(col_width, self.line_height - 1, f"Observação Validação:\n{observacao_validacao or 'Nenhuma'}", border=1)
            y_after_right = pdf.get_y()

            pdf.set_y(max(y_after_left, y_after_right))
            pdf.ln(2)

            pdf.cell(col_width, self.line_height, f"Colaborador: {colaborador}", border=1)
            pdf.set_x(pdf.l_margin + col_width + gap)
            pdf.cell(col_width, self.line_height, f"Validador: {validador}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            pdf.cell(col_width, self.line_height, f"Perguntas OK: {complemento_checklist['perguntas']['ok']} / NOK: {complemento_checklist['perguntas']['nok']}", border=1)
            pdf.set_x(pdf.l_margin + col_width + gap)
            pdf.cell(col_width, self.line_height, f"Fotos OK: {complemento_checklist['totalFotos']['ok']} / NOK: {complemento_checklist['totalFotos']['nok']}", border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(8)

            with ThreadPoolExecutor() as executor:
                for item in items:
                    conforme = item.get('conforme')
                    problema = (item.get('problema_identificado', '') or 'Nenhum').strip().replace("\n", " ")
                    imagens = item.get('imagens', []) or []

                    rotulo = item.get('item') or item.get('label') or ''

                    item_full_text = (
                        f"Item: {rotulo}\n"
                        f"Status: {'Conforme' if conforme == 1 else 'Não Conforme'}\n"
                        f"Problema(s): {problema}"
                    )

                    available_width = pdf.w - pdf.l_margin - pdf.r_margin
                    num_lines = self._get_text_line_count(pdf, item_full_text, available_width)
                    text_height = num_lines * 5

                    image_rows = (len(imagens) + 2) // 3
                    images_height = image_rows * (self.img_height + self.img_margin)

                    block_height = text_height + images_height + 15

                    if pdf.get_y() + block_height > pdf.page_break_trigger:
                        pdf.add_page()

                    fill_color = (230, 240, 255) if conforme == 1 else (255, 200, 200)
                    pdf.set_fill_color(*fill_color)

                    pdf.multi_cell(w=0, h=5, txt=item_full_text, border=0, align='L', fill=True)

                    if imagens:
                        pdf.ln(2)
                        x_start = pdf.get_x()
                        col_count = 0

                        # Coletar GCS (bucket,obj), paths no bucket padrão e URLs HTTP
                        gcs_targets: List[Tuple[str, str]] = []
                        raw_paths = []  # caminhos relativos ao bucket padrão
                        url_candidates = []  # URLs não-GCS para HTTP
                        for img in imagens:
                            p = img.get('img_path')
                            u = img.get('img_url') or img.get('url')
                            if p:
                                raw_paths.append(p)
                            if u:
                                gcs_info = self._parse_gcs_url(u)
                                if gcs_info:
                                    gcs_targets.append(gcs_info)
                                else:
                                    url_candidates.append(u)

                        image_data_list: List[bytes] = []
                        if raw_paths:
                            image_data_list.extend([d for d in self.download_images_batch(raw_paths) if d])
                        if gcs_targets:
                            image_data_list.extend([d for d in self.download_gcs_targets_batch(gcs_targets) if d])
                        if url_candidates:
                            image_data_list.extend([d for d in self.download_urls_batch(url_candidates) if d])

                        processed_images = list(executor.map(self.quick_reencode_jpg, image_data_list)) if image_data_list else []

                        for img_data in processed_images:
                            if not img_data:
                                continue
                            if col_count == 3:
                                pdf.ln(self.img_height + self.img_margin)
                                pdf.set_x(x_start)
                                col_count = 0
                            with io.BytesIO(img_data) as img_buffer:
                                pdf.image(img_buffer, x=pdf.get_x(), y=pdf.get_y(), w=self.img_width, h=self.img_height)
                            pdf.set_x(pdf.get_x() + self.img_width + self.img_margin)
                            col_count += 1

                        if col_count > 0:
                            pdf.ln(self.img_height + 4)
                        else:
                            pdf.ln(5)
                    else:
                        pdf.ln(5)

                    pdf.ln(4)

            pdf_bytes = pdf.output()
            if isinstance(pdf_bytes, bytearray):
                pdf_bytes = bytes(pdf_bytes)
            return pdf_bytes

        except Exception as e:
            print(f"ERRO CRÍTICO ao gerar PDF PGR para a chave {key}: {str(e)}")
            print(traceback.format_exc())
            raise


generator = PgrChecklistPDFGenerator()


def main(request: Request):
    try:
        key = request.args.get('key')
        if not key:
            return Response("Parâmetro 'key' obrigatório", status=400)

        request_data = request.get_json()
        if not request_data:
            return Response("JSON do corpo obrigatório", status=400)

        pdf_bytes = generator.generate_pdf(request_data, key)

        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={"Content-Disposition": f'attachment; filename="checklist-pgr-{key}.pdf"'}
        )
    except ValueError as ve:
        return Response(str(ve), status=400)
    except Exception as e:
        return Response(f"Erro interno: {str(e)}", status=500)
