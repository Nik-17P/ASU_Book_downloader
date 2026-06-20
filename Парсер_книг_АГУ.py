#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер электронных книг с elibrary.asu.ru
Автоматически определяет параметры книги и сохраняет её в PDF.
"""

import requests
import re
import os
import sys
import time
import urllib3
from io import BytesIO
from urllib.parse import urljoin, urlparse, parse_qs
from PIL import Image

# Отключаем предупреждения о SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BookParser:
    """Парсер электронных книг библиотеки АлтГУ"""
    
    def __init__(self, viewer_url, output_dir="downloaded_books"):
        self.viewer_url = viewer_url
        self.output_dir = output_dir
        self.session = requests.Session()
        # Отключаем проверку SSL-сертификатов
        self.session.verify = False
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
        })
        
        self.book_id = None
        self.book_name = None
        self.total_pages = None
        self.page_url_template = None
        self.base_viewer_url = None
        
    def _request_with_retry(self, url, retries=3, delay=2, **kwargs):
        """Выполнить запрос с повторными попытками при ошибке"""
        # Принудительно отключаем проверку SSL
        kwargs['verify'] = False
        
        for attempt in range(retries):
            try:
                response = self.session.get(url, timeout=30, **kwargs)
                if response.status_code == 200:
                    return response
                print(f"  ⚠ Статус {response.status_code}, попытка {attempt + 1}/{retries}")
            except requests.RequestException as e:
                print(f"  ⚠ Ошибка: {e}, попытка {attempt + 1}/{retries}")
            
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
        
        return None
    
    def _extract_page_url_from_getPageURI(self, html):
        """Извлечь базовый URL страницы из функции getPageURI."""
        # Паттерн 1: var url = '...'
        match = re.search(r'br\.getPageURI\s*=\s*function[^{]*\{[^}]*?var\s+url\s*=\s*["\']([^"\']+)["\']', html, re.DOTALL)
        if match:
            return match.group(1)
        
        # Паттерн 2: return '...'
        match = re.search(r'br\.getPageURI\s*=\s*function[^{]*\{[^}]*?return\s+["\']([^"\']+)["\']', html, re.DOTALL)
        if match:
            return match.group(1)
        
        # Паттерн 3: ищем любую строку с URL, содержащую name= и id=
        match = re.search(r'["\'](https?://[^"\']*\?[^"\']*name=[^"\']*id=[^"\']*)["\']', html)
        if match:
            return match.group(1)
        
        # Паттерн 4: ищем URL с параметрами page=
        match = re.search(r'["\'](https?://[^"\']*\?[^"\']*page=[^"\']*)["\']', html)
        if match:
            return match.group(1)
        
        return None
    
    def _clean_page_url(self, url):
        """Очистить URL страницы от динамических параметров."""
        # Убираем параметр page=N
        url = re.sub(r'[?&]page=\d+', '', url)
        url = re.sub(r'[?&]page=["\']?\s*\+\s*["\']?[^&"\']+', '', url)
        
        # Убираем параметр mode=...
        url = re.sub(r'[?&]mode=["\']?\s*\+\s*["\']?[^&"\']+', '', url)
        url = re.sub(r'[?&]mode=\w+', '', url)
        
        # Убираем trailing & или ?
        url = re.sub(r'[?&]$', '', url)
        url = url.rstrip('?&')
        
        return url
    
    def _normalize_url_scheme(self, url):
        """
        Нормализовать схему URL.
        Если исходный URL был HTTP, а в getPageURI HTTPS - заменяем на HTTP.
        """
        # Определяем схему исходного URL
        parsed_viewer = urlparse(self.viewer_url)
        original_scheme = parsed_viewer.scheme
        
        # Если в шаблоне URL схема отличается - заменяем
        parsed_template = urlparse(url)
        if parsed_template.scheme != original_scheme:
            url = url.replace(f"{parsed_template.scheme}://", f"{original_scheme}://", 1)
        
        return url
    
    def detect_book_parameters(self):
        """Автоматически определить параметры книги по URL просмотрщика."""
        print(f"📖 Анализ книги: {self.viewer_url}")
        print("=" * 60)
        
        # Шаг 1: Получаем главную страницу
        print("→ Получение главной страницы...")
        response = self._request_with_retry(self.viewer_url)
        if not response:
            raise Exception("Не удалось получить главную страницу книги")
        
        main_html = response.text
        self.base_viewer_url = response.url
        
        # Шаг 2: Ищем фрейм с просмотрщиком
        frame_match = re.search(r'<frame[^>]+src=["\']([^"\']+)["\']', main_html)
        
        if frame_match:
            viewer_frame_url = frame_match.group(1)
            viewer_frame_url = urljoin(self.base_viewer_url, viewer_frame_url)
            print(f"→ Найдена страница просмотрщика: {viewer_frame_url}")
        else:
            viewer_frame_url = self.base_viewer_url
            print("→ Страница просмотрщика обнаружена напрямую")
        
        # Шаг 3: Получаем параметры из URL
        parsed = urlparse(viewer_frame_url)
        query_params = parse_qs(parsed.query)
        
        if 'id' in query_params:
            self.book_id = int(query_params['id'][0])
        else:
            id_match = re.search(r'[?&]id=(\d+)', viewer_frame_url)
            if id_match:
                self.book_id = int(id_match.group(1))
        
        if 'name' in query_params:
            self.book_name = query_params['name'][0]
        else:
            name_match = re.search(r'[?&]name=([^&"\']+)', viewer_frame_url)
            if name_match:
                self.book_name = name_match.group(1)
        
        print(f"→ ID книги: {self.book_id}")
        print(f"→ Имя файла: {self.book_name}")
        
        # Шаг 4: Получаем HTML просмотрщика
        print("→ Анализ просмотрщика...")
        self.session.headers['Referer'] = self.base_viewer_url
        
        viewer_response = self._request_with_retry(viewer_frame_url)
        if not viewer_response:
            raise Exception("Не удалось получить страницу просмотрщика")
        
        viewer_html = viewer_response.text
        
        # Проверяем на ошибку авторизации
        if 'error' in viewer_html.lower() and ('ошибка' in viewer_html.lower() or 'authorization' in viewer_html.lower()):
            raise Exception("Ошибка авторизации при доступе к просмотрщику")
        
        # Ищем количество страниц
        pages_patterns = [
            r'br\.numLeafs\s*=\s*(\d+)',
            r'br\.numLeafs=(\d+)',
            r'numLeafs\s*[:=]\s*(\d+)',
        ]
        
        for pattern in pages_patterns:
            pages_match = re.search(pattern, viewer_html)
            if pages_match:
                self.total_pages = int(pages_match.group(1))
                print(f"→ Количество страниц: {self.total_pages}")
                break
        
        if not self.total_pages:
            raise Exception("Не удалось определить количество страниц")
        
        # Извлекаем URL страницы из функции getPageURI
        page_url_base = self._extract_page_url_from_getPageURI(viewer_html)
        
        if page_url_base:
            print(f"→ Найден URL в getPageURI: {page_url_base}")
            # Очищаем URL от динамических параметров
            self.page_url_template = self._clean_page_url(page_url_base)
            
            # Нормализуем схему URL (HTTP/HTTPS)
            self.page_url_template = self._normalize_url_scheme(self.page_url_template)
            
            # Добавляем параметр page если его нет
            if 'page=' not in self.page_url_template:
                sep = '&' if '?' in self.page_url_template else '?'
                self.page_url_template = f"{self.page_url_template}{sep}page="
            
            print(f"→ Шаблон URL страниц: {self.page_url_template}{{N}}")
        else:
            # Fallback: ищем любые URL с name= и id=
            url_matches = re.findall(
                r'["\'](https?://[^"\']*\?(?:[^"\']*name=[^"\']*id=[^"\']*|[^"\']*id=[^"\']*name=[^"\']*))["\']',
                viewer_html
            )
            if url_matches:
                page_url_base = url_matches[0]
                self.page_url_template = self._clean_page_url(page_url_base)
                self.page_url_template = self._normalize_url_scheme(self.page_url_template)
                
                if 'page=' not in self.page_url_template:
                    sep = '&' if '?' in self.page_url_template else '?'
                    self.page_url_template = f"{self.page_url_template}{sep}page="
                
                print(f"→ Шаблон URL страниц (fallback): {self.page_url_template}{{N}}")
            else:
                raise Exception("Не удалось определить шаблон URL для загрузки страниц")
        
        if not all([self.book_id, self.book_name, self.total_pages, self.page_url_template]):
            raise Exception("Не удалось определить все параметры книги")
        
        print("=" * 60)
        print("✅ Параметры книги успешно определены\n")
        
        return {
            'book_id': self.book_id,
            'book_name': self.book_name,
            'total_pages': self.total_pages
        }
    
    def get_page_url(self, page_num):
        """Получить URL для конкретной страницы"""
        return f"{self.page_url_template}{page_num}"
    
    def download_page(self, page_num):
        """Скачать страницу как изображение"""
        url = self.get_page_url(page_num)
        
        self.session.headers['Referer'] = self.base_viewer_url
        
        response = self._request_with_retry(url)
        if response and response.content:
            if len(response.content) > 1000:
                return response.content
        return None
    
    def download_all_pages(self, start_page=1, end_page=None, delay=0.5):
        """Скачать все страницы книги."""
        if end_page is None:
            end_page = self.total_pages
        
        book_dir = os.path.join(self.output_dir, f"book_{self.book_id}")
        os.makedirs(book_dir, exist_ok=True)
        
        images = []
        total_to_download = end_page - start_page + 1
        success_count = 0
        fail_count = 0
        
        print(f"📥 Загрузка страниц {start_page}-{end_page} из {self.total_pages}")
        print(f"📁 Директория: {book_dir}\n")
        
        for page in range(start_page, end_page + 1):
            progress = page - start_page + 1
            percent = (progress / total_to_download) * 100
            
            filename = os.path.join(book_dir, f"page_{page:04d}.png")
            if os.path.exists(filename) and os.path.getsize(filename) > 1000:
                with open(filename, 'rb') as f:
                    image_data = f.read()
                img = Image.open(BytesIO(image_data))
                images.append((page, img))
                print(f"\r[{progress}/{total_to_download}] ({percent:5.1f}%) Стр. {page}: ✓ (из кэша)", end="")
                continue
            
            image_data = self.download_page(page)
            
            if image_data:
                with open(filename, 'wb') as f:
                    f.write(image_data)
                
                try:
                    img = Image.open(BytesIO(image_data))
                    images.append((page, img))
                    success_count += 1
                    print(f"\r[{progress}/{total_to_download}] ({percent:5.1f}%) Стр. {page}: ✓ ({len(image_data)//1024} KB)", end="")
                except Exception as e:
                    fail_count += 1
                    print(f"\r[{progress}/{total_to_download}] ({percent:5.1f}%) Стр. {page}: ✗ (ошибка изображения)")
            else:
                fail_count += 1
                print(f"\r[{progress}/{total_to_download}] ({percent:5.1f}%) Стр. {page}: ✗ (не загружена)", end="")
            
            time.sleep(delay)
        
        print(f"\n\n📊 Результат загрузки:")
        print(f"   ✓ Успешно: {success_count}")
        print(f"   ✗ Ошибок: {fail_count}")
        print(f"   📁 Всего изображений: {len(images)}")
        
        return images, book_dir
    
    def save_as_pdf(self, images, output_filename, quality=85):
        """Сохранить изображения в PDF."""
        if not images:
            print("❌ Нет изображений для сохранения в PDF!")
            return None
        
        images.sort(key=lambda x: x[0])
        
        print(f"\n📄 Сохранение в PDF: {output_filename}")
        print(f"   Страниц: {len(images)}")
        
        pdf_images = []
        for page_num, img in images:
            try:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                pdf_images.append(img)
            except Exception as e:
                print(f"   ⚠ Ошибка обработки страницы {page_num}: {e}")
        
        if not pdf_images:
            print("❌ Не удалось подготовить ни одного изображения!")
            return None
        
        try:
            first_image = pdf_images[0]
            rest_images = pdf_images[1:]
            
            first_image.save(
                output_filename,
                "PDF",
                resolution=150.0,
                save_all=True,
                append_images=rest_images
            )
            
            file_size = os.path.getsize(output_filename)
            size_mb = file_size / 1024 / 1024
            
            print(f"✅ PDF успешно сохранён!")
            print(f"   📁 Файл: {output_filename}")
            print(f"   📏 Размер: {size_mb:.2f} MB")
            print(f"   📄 Страниц: {len(pdf_images)}")
            
            return output_filename
            
        except Exception as e:
            print(f"❌ Ошибка при сохранении PDF: {e}")
            return None
    
    def parse_and_save(self, output_filename=None, start_page=1, end_page=None, delay=0.5):
        """Полный цикл: определение параметров, скачивание и сохранение в PDF."""
        try:
            self.detect_book_parameters()
            
            if output_filename is None:
                output_filename = f"book_{self.book_id}.pdf"
            
            images, book_dir = self.download_all_pages(start_page, end_page, delay)
            
            if images:
                pdf_path = os.path.join(book_dir, output_filename)
                return self.save_as_pdf(images, pdf_path)
            else:
                print("❌ Не удалось загрузить ни одной страницы!")
                return None
                
        except Exception as e:
            print(f"\n❌ Критическая ошибка: {e}")
            import traceback
            traceback.print_exc()
            return None


def main():
    """Главная функция"""
    default_url = "http://elibrary.asu.ru/xmlui/bitstream/handle/asu/11153/read.7book?sequence=1&isAllowed=y"
    
    import argparse
    parser = argparse.ArgumentParser(
        description="Парсер электронных книг elibrary.asu.ru",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--url', '-u', default=default_url, help='URL страницы просмотра книги')
    parser.add_argument('--pages', '-p', default=None, help='Диапазон страниц (например: 1-50)')
    parser.add_argument('--delay', '-d', type=float, default=0.5, help='Задержка между запросами (сек)')
    parser.add_argument('--output', '-o', default=None, help='Имя выходного PDF файла')
    parser.add_argument('--output-dir', default="downloaded_books", help='Директория для сохранения')
    
    args = parser.parse_args()
    
    start_page = 1
    end_page = None
    if args.pages:
        try:
            parts = args.pages.split('-')
            if len(parts) == 2:
                start_page = int(parts[0])
                end_page = int(parts[1])
            elif len(parts) == 1:
                start_page = end_page = int(parts[0])
        except ValueError:
            print(f"❌ Неверный формат диапазона страниц: {args.pages}")
            sys.exit(1)
    
    print("🚀 Парсер электронных книг elibrary.asu.ru")
    print("=" * 60)
    
    book_parser = BookParser(
        viewer_url=args.url,
        output_dir=args.output_dir
    )
    
    result = book_parser.parse_and_save(
        output_filename=args.output,
        start_page=start_page,
        end_page=end_page,
        delay=args.delay
    )
    
    if result:
        print("\n🎉 Готово! Книга успешно сохранена.")
        sys.exit(0)
    else:
        print("\n❌ Не удалось сохранить книгу.")
        sys.exit(1)


if __name__ == "__main__":
    main()