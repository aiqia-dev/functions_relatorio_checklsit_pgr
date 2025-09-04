import os
import io
from datetime import datetime
from typing import Dict, Optional, List, Tuple

from google.cloud import storage
from PIL import Image, ImageDraw
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

    def _color_tuple(self, color: str) -> Tuple[int, int, int]:
        m = (color or '').strip().lower()
        mapping = {
            'red': (255, 0, 0),
            'green': (0, 200, 0),
            'blue': (0, 0, 255),
            'yellow': (255, 200, 0),
            'orange': (255, 140, 0),
            'purple': (160, 32, 240),
            'white': (255, 255, 255),
            'black': (0, 0, 0),
        }
        return mapping.get(m, (255, 0, 0))

    def _apply_annotations(self, image_data: bytes, annotations: List[Dict]) -> bytes:
        """Aplica retângulos/pontos/círculos na imagem conforme 'annotations'."""
        try:
            with Image.open(io.BytesIO(image_data)) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                draw = ImageDraw.Draw(img)
                for ann in annotations or []:
                    try:
                        ann_type = (ann.get('annotationType') or ann.get('type') or 'box').strip().lower()
                        coords_raw = ann.get('coordinates')
                        color = self._color_tuple(ann.get('color') or 'red')
                        width = 3
                        coords = {}
                        if isinstance(coords_raw, dict):
                            coords = coords_raw
                        elif isinstance(coords_raw, str):
                            import json
                            coords = json.loads(coords_raw)
                        x = float(coords.get('x', 0))
                        y = float(coords.get('y', 0))
                        w = float(coords.get('w', 0))
                        h = float(coords.get('h', 0))

                        if ann_type in ('box', 'rectangle', 'rect'):
                            draw.rectangle([(x, y), (x + w, y + h)], outline=color, width=width)
                        elif ann_type in ('circle', 'ellipse'):
                            draw.ellipse([(x, y), (x + w, y + h)], outline=color, width=width)
                        elif ann_type in ('point', 'dot'):
                            r = max(3.0, min(8.0, w or 5.0))
                            draw.ellipse([(x - r, y - r), (x + r, y + r)], fill=color, outline=color, width=1)
                        else:
                            draw.rectangle([(x, y), (x + w, y + h)], outline=color, width=width)
                    except Exception as e:
                        print(f"Alerta: falha ao desenhar anotação: {str(e)}")
                out = io.BytesIO()
                img.save(out, format='JPEG', quality=85, optimize=True)
                return out.getvalue()
        except Exception as e:
            print(f"Alerta: não foi possível aplicar anotações: {str(e)}")
            return image_data

    def _fetch_single_image_bytes(self, img_obj: Dict) -> Optional[bytes]:
        """Obtém os bytes de uma imagem considerando img_path, URLs GCS ou HTTP."""
        try:
            p = img_obj.get('img_path')
            u = img_obj.get('img_url') or img_obj.get('url')
            if p:
                blob = self.bucket.blob(p)
                if blob.exists():
                    return blob.download_as_bytes()
                print(f"Alerta: Imagem não encontrada no GCS (bucket padrão): {p}")
            if u:
                gcs_info = self._parse_gcs_url(u)
                if gcs_info:
                    bucket_name, object_path = gcs_info
                    bkt = self.gcs_client.bucket(bucket_name)
                    blob = bkt.blob(object_path)
                    if blob.exists():
                        return blob.download_as_bytes()
                    print(f"Alerta: Imagem GCS não encontrada: gs://{bucket_name}/{object_path}")
                else:
                    return self._download_single_url(u)
            return None
        except Exception as e:
            print(f"Erro ao obter imagem: {str(e)}")
            return None

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
        if date_str is None:
            return 'N/A'
        s = str(date_str).strip()
        if not s or s.lower() in ('n/a', 'na', 'none', 'null', '-'):
            return 'N/A'
        for fmt in (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%fZ',
        ):
            try:
                return datetime.strptime(s, fmt).strftime('%d-%m-%Y %H:%M:%S')
            except Exception:
                continue
        return s

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

            # Header com título centralizado (sem logo)
            header_y = 10
            pdf.set_font("helvetica", style="B", size=14)
            title = "Checklist PGR"
            pdf.set_xy(0, header_y + 2)
            pdf.cell(w=pdf.w, h=8, txt=title, border=0, align='C')
            pdf.set_font("helvetica", size=10)

            # Linha separadora sutil
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, header_y + 12, pdf.w - pdf.r_margin, header_y + 12)

            # Bloco de metadados
            pdf.set_y(header_y + 16)
            pdf.set_x(pdf.l_margin)

            # Layout: Código ocupa 2 colunas (antes: Código+KM) e as datas ficam empilhadas na coluna da direita
            page_width = pdf.w - pdf.l_margin - pdf.r_margin
            left_block_w = 120  # 60 (Código) + 60 (KM que foi removido)
            if left_block_w > page_width * 0.7:
                left_block_w = int(page_width * 0.6)

            # Primeira linha: Código (esquerda larga) + Data Execução (direita)
            pdf.cell(left_block_w, self.line_height, f"Código: {key}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(0, self.line_height, f"Data Execução: {run_date}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            # Segunda linha: Placa + Tipo à esquerda e Data Validação à direita na MESMA linha
            pdf.set_x(pdf.l_margin)
            pdf.cell(60, self.line_height, f"Placa: {placa}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.cell(60, self.line_height, f"Tipo: {tipo_evento}", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_x(pdf.l_margin + left_block_w)
            pdf.cell(0, self.line_height, f"Data Validação: {data_validacao}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)

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
                    # 'budget' não é monetário; significa contorno/box do status — já será aplicado no badge

                    rotulo = item.get('item') or item.get('label') or ''

                    # Título do item
                    item_title_line = f"Item: {rotulo}"
                    problema_line = f"Problema(s): {problema}"

                    available_width = pdf.w - pdf.l_margin - pdf.r_margin
                    lines_item = self._get_text_line_count(pdf, item_title_line, available_width)
                    lines_prob = self._get_text_line_count(pdf, problema_line, available_width)
                    text_height = (lines_item + lines_prob + 1) * 5  # +1 para a linha do status/badges

                    image_rows = (len(imagens) + 2) // 3
                    images_height = image_rows * (self.img_height + self.img_margin)

                    block_height = text_height + images_height + 15

                    if pdf.get_y() + block_height > pdf.page_break_trigger:
                        pdf.add_page()

                    # Linha: Item
                    pdf.multi_cell(w=0, h=5, txt=item_title_line, border=0, align='L')

                    # Linha: Status (badge discreta com contorno)
                    pdf.set_x(pdf.l_margin)
                    status_text = 'Conforme' if conforme == 1 else 'Não Conforme'
                    if conforme == 1:
                        stroke_rgb = (76, 175, 80)   # verde médio para contorno
                        text_rgb = (46, 125, 50)     # verde escuro no texto
                        bg_rgb = (245, 252, 246)     # leve fundo quase branco
                    else:
                        stroke_rgb = (229, 57, 53)   # vermelho médio para contorno
                        text_rgb = (183, 28, 28)
                        bg_rgb = (254, 246, 246)

                    pdf.set_text_color(0, 0, 0)
                    pdf.set_font("helvetica", size=10)
                    pdf.cell(pdf.get_string_width("Status: ") + 1, 5, "Status: ", border=0, align='L')

                    pad_x = 3
                    badge_h = 5
                    badge_w = pdf.get_string_width(status_text) + 2 * pad_x
                    x0 = pdf.get_x()
                    y0 = pdf.get_y()
                    # Leve fundo (opcional) e contorno
                    try:
                        pdf.set_fill_color(*bg_rgb)
                        pdf.set_draw_color(*stroke_rgb)
                        pdf.set_line_width(0.4)
                        pdf.rounded_rect(x0, y0, badge_w, badge_h, 1.5, style='FD')
                    except Exception:
                        # Fallback: célula com borda
                        pdf.set_draw_color(*stroke_rgb)
                        pdf.set_line_width(0.4)
                        pdf.rect(x0, y0, badge_w, badge_h)

                    # Texto centralizado dentro do badge
                    pdf.set_text_color(*text_rgb)
                    pdf.set_font("helvetica", style="B", size=9)
                    pdf.set_xy(x0, y0)
                    pdf.cell(badge_w, badge_h, status_text, border=0, align='C', fill=False)

                    # Tags do item como badges discretas cinza (ao lado do status), com quebra de linha automática
                    tags = item.get('tags') or []
                    if isinstance(tags, list) and tags:
                        pdf.set_font("helvetica", size=8)
                        pdf.set_text_color(80, 80, 80)
                        pdf.set_draw_color(180, 180, 180)
                        pdf.set_line_width(0.3)
                        pdf.set_x(x0 + badge_w + 3)
                        right_edge = pdf.w - pdf.r_margin
                        for t in tags:
                            try:
                                label = ''
                                if isinstance(t, dict):
                                    k = (t.get('key') or '').strip()
                                    v = (t.get('value') or '').strip()
                                    label = (f"{k}: {v}" if k and v else (v or k))
                                else:
                                    label = str(t).strip()
                                if not label:
                                    continue
                                tw = pdf.get_string_width(label)
                                pad = 2
                                bw = tw + pad * 2
                                x = pdf.get_x()
                                y = pdf.get_y()
                                if x + bw > right_edge:
                                    pdf.ln(badge_h)
                                    pdf.set_x(pdf.l_margin)
                                    x = pdf.get_x()
                                    y = pdf.get_y()
                                try:
                                    pdf.rounded_rect(x, y, bw, badge_h, 1.2, style='D')
                                except Exception:
                                    pdf.rect(x, y, bw, badge_h)
                                pdf.set_xy(x + pad, y)
                                pdf.cell(tw, badge_h, label, border=0, align='L')
                                pdf.set_x(x + bw + 2)
                            except Exception:
                                continue

                    # Quebra de linha após status/tags
                    pdf.ln(badge_h)

                    # Linha: Problema(s)
                    pdf.set_text_color(0, 0, 0)
                    pdf.set_font("helvetica", size=10)
                    pdf.multi_cell(w=0, h=5, txt=problema_line, border=0, align='L')

                    if imagens:
                        pdf.ln(2)
                        x_start = pdf.get_x()
                        col_count = 0
                        row_max_height = 0

                        processed_images_with_captions: List[Tuple[bytes, str]] = []
                        for img in imagens:
                            data = self._fetch_single_image_bytes(img)
                            if not data:
                                continue
                            annotations = img.get('annotations') or []
                            try:
                                annotated = self._apply_annotations(data, annotations) if annotations else data
                            except Exception as e:
                                print(f"Alerta: falha ao aplicar anotações, usando imagem original: {str(e)}")
                                annotated = data

                            rec = self.quick_reencode_jpg(annotated)
                            if rec is None:
                                try:
                                    Image.open(io.BytesIO(annotated)).close()
                                    final_bytes = annotated
                                except Exception:
                                    print("Alerta: bytes de imagem inválidos após fallback; imagem será ignorada")
                                    continue
                            else:
                                final_bytes = rec

                            descriptions: List[str] = []
                            for ann in annotations:
                                desc = (ann.get('description') or '').strip()
                                if desc:
                                    descriptions.append(f"- {desc}")
                            caption = "\n".join(descriptions)
                            processed_images_with_captions.append((final_bytes, caption))

                        for img_data, caption in processed_images_with_captions:
                            if not img_data:
                                continue
                            if col_count == 3:
                                pdf.ln(row_max_height + self.img_margin if row_max_height else (self.img_height + self.img_margin))
                                pdf.set_x(x_start)
                                col_count = 0
                                row_max_height = 0

                            x = pdf.get_x()
                            y = pdf.get_y()
                            try:
                                with io.BytesIO(img_data) as img_buffer:
                                    pdf.image(img_buffer, x=x, y=y, w=self.img_width, h=self.img_height)
                            except Exception as e:
                                print(f"Alerta: falha ao inserir imagem no PDF; ignorando. Erro: {str(e)}")
                                continue

                            used_height = self.img_height
                            cap = (caption or '').strip()
                            if cap:
                                pdf.set_xy(x, y + self.img_height + 1)
                                lines = self._get_text_line_count(pdf, cap.replace('\r', ''), self.img_width)
                                line_h = 4
                                pdf.set_font("helvetica", size=8)
                                pdf.multi_cell(self.img_width, line_h, cap, border=0)
                                pdf.set_font("helvetica", size=10)
                                used_height += 1 + lines * line_h

                            row_max_height = max(row_max_height, used_height)
                            pdf.set_xy(x + self.img_width + self.img_margin, y)
                            col_count += 1

                        if col_count > 0:
                            pdf.ln(row_max_height + 4 if row_max_height else (self.img_height + 4))
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


from flask import Flask, request, jsonify, Response

app = Flask(__name__)
generator = PgrChecklistPDFGenerator()

@app.route('/generate-report', methods=['POST'])
def generate_report_endpoint():
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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)
