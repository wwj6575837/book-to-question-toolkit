#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MinerU 批量PDF转换工具
支持批量上传本地PDF文件到MinerU API进行解析，并将结果保存到原位置
"""

import os
import json
import time
import zipfile
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path
from typing import List, Dict, Optional
import shutil
from datetime import datetime, timedelta
import re
from collections import defaultdict

from qiniu_markdown_images import process_extract_directory
from project_dotenv import load_project_dotenv


class MinerUBatchConverter:
    # MinerU API 限制常量（与 md/MinerUAPI.md 精准解析 API 一致）
    MAX_FILE_SIZE_MB = 200
    MAX_PAGES_PER_FILE = 200
    # 文档「本地文件批量上传解析」：单次申请上传链接不能超过 50 个（/file-urls/batch）
    MAX_FILES_PER_FILE_URLS_BATCH = 50
    DAILY_FREE_PAGES = 1000  # 文档：每日 1000 页高优先级额度，超出部分优先级降低
    UPLOAD_URL_VALID_HOURS = 24

    # 错误码映射
    ERROR_CODES = {
        'A0202': 'Token 错误',
        'A0211': 'Token 过期',
        '-500': '传参错误',
        '-10001': '服务异常',
        '-10002': '请求参数错误',
        '-60001': '生成上传 URL 失败，请稍后再试',
        '-60002': '获取匹配的文件格式失败',
        '-60003': '文件读取失败',
        '-60004': '空文件',
        '-60005': '文件大小超出限制',
        '-60006': '文件页数超过限制',
        '-60007': '模型服务暂时不可用',
        '-60008': '文件读取超时',
        '-60009': '任务提交队列已满',
        '-60010': '解析失败',
        '-60011': '获取有效文件失败',
        '-60012': '找不到任务',
        '-60013': '没有权限访问该任务',
        '-60014': '删除运行中的任务',
        '-60015': '文件转换失败',
        '-60016': '文件转换失败',
        '-60017': '重试次数达到上限',
        '-60018': '每日解析任务数量已达上限',
        '-60019': 'html文件解析额度不足',
        '-60020': '文件拆分失败',
        '-60021': '读取文件页数失败',
        '-60022': '网页读取失败',
    }

    def __init__(
            self,
            api_token: str,
            advanced: Optional[Dict] = None,
            qiniu: Optional[Dict] = None,
    ):
        """
        初始化MinerU批量转换器

        Args:
            api_token: MinerU API Token
            qiniu: 可选七牛配置（见 config.json qiniu 段），密钥来自环境变量
        """
        self.api_token = api_token
        self.qiniu = qiniu or {}
        self.base_url = "https://mineru.net/api/v4"
        self.headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_token}'
        }
        self.upload_start_time = None  # 记录上传链接申请时间
        # 高级参数
        self.advanced = advanced or {}
        self.download_retries = int(self.advanced.get('download_retries', 3))
        # 独立的下载超时（秒）；若未配置则与上传超时保持一致
        self.download_timeout = int(self.advanced.get('download_timeout', self.advanced.get('upload_timeout', 300)))
        self.upload_timeout = int(self.advanced.get('upload_timeout', 300))
        self.download_chunk_size = int(self.advanced.get('download_chunk_size', 1024 * 1024))  # 1MB
        # 分段下载的段大小，默认 8MB，可通过 advanced.download_segment_bytes 调整
        self.segment_bytes = int(self.advanced.get('download_segment_bytes', 8 * 1024 * 1024))
        # 可选：强制使用分段下载，默认 False
        self.force_segment_download = bool(self.advanced.get('force_segment_download', False))
        # /file-urls/batch 单次文件数上限（官方 50）；勿超过以免被拒
        _bs = int(self.advanced.get('file_urls_batch_size', self.MAX_FILES_PER_FILE_URLS_BATCH))
        self.file_urls_batch_size = max(1, min(_bs, self.MAX_FILES_PER_FILE_URLS_BATCH))
        # 复用会话，附带自动重试
        self.session = self._build_retry_session()

    def _build_retry_session(self) -> requests.Session:
        """
        创建带有退避重试策略的请求会话，用于下载阶段缓解偶发的 TLS 断连（SSLEOFError）
        """
        session = requests.Session()
        # 对于下载会话，禁用从系统环境读取代理，避免被 Clash 等代理劫持
        session.trust_env = False
        retry = Retry(
            total=self.download_retries,
            connect=self.download_retries,
            read=self.download_retries,
            status=self.download_retries,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'HEAD']),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def _download_file_with_resume(self, url: str, dest_path: Path) -> bool:
        """
        具备断点续传与稳健重试的下载器，针对 CDN 偶发 [SSL: UNEXPECTED_EOF_WHILE_READING] 进行恢复
        """
        # 如配置强制分段下载，则直接走分段流程
        if self.force_segment_download:
            return self._download_file_in_segments(url, dest_path)
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        attempts = self.download_retries
        for attempt in range(1, attempts + 1):
            try:
                downloaded = dest_path.stat().st_size if dest_path.exists() else 0
                headers = {
                    'Accept-Encoding': 'identity',
                    'User-Agent': 'MinerU-Downloader/1.0',
                    'Connection': 'close',
                }
                if downloaded > 0:
                    headers['Range'] = f'bytes={downloaded}-'

                resp = self.session.get(
                    url,
                    headers=headers,
                    timeout=(10, self.download_timeout),
                    stream=True,
                    proxies={},  # 显式禁用本次请求的代理
                )

                if resp.status_code == 206:
                    content_range = resp.headers.get('Content-Range', '')
                    total_str = content_range.split('/')[-1] if '/' in content_range else ''
                    total_size = int(total_str) if total_str.isdigit() else None
                    mode = 'ab'
                elif resp.status_code == 200:
                    total_size = int(resp.headers.get('Content-Length', 0)) or None
                    downloaded = 0
                    mode = 'wb'
                else:
                    print(f"   下载失败，状态码: {resp.status_code}")
                    if attempt < attempts:
                        sleep_s = min(30, 2 ** attempt)
                        print(f"   下载重试 {attempt}/{attempts}: {resp.status_code}，{sleep_s}s 后重试")
                        time.sleep(sleep_s)
                        continue
                    return False

                with open(dest_path, mode) as f:
                    for chunk in resp.iter_content(chunk_size=self.download_chunk_size):
                        if chunk:
                            f.write(chunk)

                final_size = dest_path.stat().st_size
                size_ok = True
                if total_size is not None and final_size < total_size:
                    size_ok = False
                else:
                    try:
                        with zipfile.ZipFile(dest_path, 'r') as zf:
                            bad = zf.testzip()
                            if bad is not None:
                                size_ok = False
                    except zipfile.BadZipFile:
                        size_ok = False

                if size_ok:
                    return True

                if attempt < attempts:
                    sleep_s = min(30, 2 ** attempt)
                    print(f"   下载内容不完整，准备续传 ({attempt}/{attempts})，{sleep_s}s 后重试")
                    time.sleep(sleep_s)
                    continue
                else:
                    return False

            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.Timeout,
                    requests.exceptions.SSLError) as e:
                if attempt < attempts:
                    sleep_s = min(30, 2 ** attempt)
                    print(f"   下载重试 {attempt}/{attempts}: {str(e)}，{sleep_s}s 后重试")
                    time.sleep(sleep_s)
                    continue
                else:
                    print(f"   下载失败: {str(e)}")
                    print("   正在切换为分段下载策略...")
                    return self._download_file_in_segments(url, dest_path)
            except Exception as e:
                print(f"   下载发生异常: {str(e)}")
                return False

        return False

    def _download_file_in_segments(self, url: str, dest_path: Path) -> bool:
        """
        分段 Range 下载，减少长连接传输导致的 TLS EOF 风险。
        将文件按 segment_bytes 切片逐段拉取并写入相应偏移。
        """
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # 获取总大小
        total_size = None
        try:
            # 优先尝试 HEAD
            head = self.session.head(url, timeout=(10, 30), allow_redirects=True, proxies={})
            if head.status_code // 100 == 2:
                total_size = int(head.headers.get('Content-Length', '0') or 0) or None
        except Exception:
            total_size = None

        if total_size is None:
            # 用 Range 探测
            try:
                probe = self.session.get(
                    url,
                    headers={'Range': 'bytes=0-0', 'Accept-Encoding': 'identity', 'Connection': 'close',
                             'User-Agent': 'MinerU-Downloader/1.0'},
                    timeout=(10, 30),
                    stream=True,
                    proxies={},
                )
                if probe.status_code in (200, 206):
                    cr = probe.headers.get('Content-Range')
                    if cr and '/' in cr:
                        total_str = cr.split('/')[-1]
                        total_size = int(total_str) if total_str.isdigit() else None
                    elif probe.headers.get('Content-Length'):
                        total_size = int(probe.headers['Content-Length'])
            except Exception:
                pass

        # 如果仍未知长度，退回到整流下载一次
        if total_size is None:
            return self._download_file_with_resume(url, dest_path)

        # 计算已下载大小并从对应段继续
        downloaded = dest_path.stat().st_size if dest_path.exists() else 0
        start_seg = downloaded // self.segment_bytes
        total_segs = (total_size + self.segment_bytes - 1) // self.segment_bytes

        # 逐段下载
        try:
            # 确保文件存在
            if not dest_path.exists():
                with open(dest_path, 'wb') as _:
                    pass

            for seg in range(start_seg, total_segs):
                seg_start = seg * self.segment_bytes
                seg_end = min(total_size - 1, seg_start + self.segment_bytes - 1)
                headers = {
                    'Range': f'bytes={seg_start}-{seg_end}',
                    'Accept-Encoding': 'identity',
                    'Connection': 'close',
                    'User-Agent': 'MinerU-Downloader/1.0',
                }

                # 段内重试
                for attempt in range(1, self.download_retries + 1):
                    try:
                        resp = self.session.get(url, headers=headers, timeout=(10, self.download_timeout), stream=True,
                                                proxies={})
                        if resp.status_code not in (200, 206):
                            raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}")

                        with open(dest_path, 'r+b') as f:
                            f.seek(seg_start)
                            for chunk in resp.iter_content(chunk_size=self.download_chunk_size):
                                if chunk:
                                    f.write(chunk)
                        break  # 当前段成功
                    except (requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ReadTimeout,
                            requests.exceptions.Timeout,
                            requests.exceptions.SSLError,
                            requests.exceptions.HTTPError) as e:
                        if attempt < self.download_retries:
                            sleep_s = min(30, 2 ** attempt)
                            print(
                                f"   分段下载重试 段{seg + 1}/{total_segs} 第{attempt}/{self.download_retries}次: {e}，{sleep_s}s 后重试")
                            time.sleep(sleep_s)
                            continue
                        else:
                            print(f"   分段下载失败 段{seg + 1}/{total_segs}: {e}")
                            return False

            # 校验完整性
            final_size = dest_path.stat().st_size
            if final_size != total_size:
                print(f"   分段下载后大小不一致: {final_size} != {total_size}")
                return False
            try:
                with zipfile.ZipFile(dest_path, 'r') as zf:
                    if zf.testzip() is not None:
                        print("   分段下载后ZIP校验失败")
                        return False
            except zipfile.BadZipFile:
                print("   分段下载后ZIP解析失败")
                return False

            return True
        except Exception as e:
            print(f"   分段下载发生异常: {e}")
            return False

    def validate_file(self, file_path: Path) -> Dict[str, any]:
        """
        验证单个文件是否符合MinerU限制

        Args:
            file_path: 文件路径

        Returns:
            验证结果字典 {"valid": bool, "reason": str, "size_mb": float}
        """
        try:
            # 检查文件是否存在
            if not file_path.exists():
                return {"valid": False, "reason": "文件不存在", "size_mb": 0}

            # 检查文件大小
            file_size_bytes = file_path.stat().st_size
            file_size_mb = file_size_bytes / (1024 * 1024)

            if file_size_mb > self.MAX_FILE_SIZE_MB:
                return {
                    "valid": False,
                    "reason": f"文件大小 {file_size_mb:.1f}MB 超过限制 {self.MAX_FILE_SIZE_MB}MB",
                    "size_mb": file_size_mb
                }

            # 检查文件是否为空
            if file_size_bytes == 0:
                return {"valid": False, "reason": "文件为空", "size_mb": 0}

            return {"valid": True, "reason": "", "size_mb": file_size_mb}

        except Exception as e:
            return {"valid": False, "reason": f"文件检查失败: {str(e)}", "size_mb": 0}

    def estimate_pdf_pages(self, file_path: Path) -> int:
        """
        估算PDF文件页数（简单估算，基于文件大小）
        注意：这只是粗略估算，实际页数可能不同

        Args:
            file_path: PDF文件路径

        Returns:
            估算的页数
        """
        try:
            file_size_mb = file_path.stat().st_size / (1024 * 1024)
            # 简单估算：平均每页约0.1-0.5MB，取中间值0.3MB
            estimated_pages = max(1, int(file_size_mb / 0.3))

            # 限制在合理范围内
            if estimated_pages > self.MAX_PAGES_PER_FILE * 2:
                estimated_pages = self.MAX_PAGES_PER_FILE + 1  # 标记为可能超出

            return estimated_pages
        except:
            return 1

    def find_pdf_files(self, directory: str, recursive: bool = True, validate: bool = True) -> List[Path]:
        """
        查找指定目录下的PDF文件并验证

        Args:
            directory: 搜索目录
            recursive: 是否递归搜索子目录
            validate: 是否验证文件

        Returns:
            有效的PDF文件路径列表
        """
        pdf_files = []
        directory_path = Path(directory)

        if not directory_path.exists():
            print(f"✗ 目录不存在: {directory}")
            return []

        # 查找PDF文件
        if recursive:
            all_pdf_files = list(directory_path.rglob("*.pdf"))
        else:
            all_pdf_files = list(directory_path.glob("*.pdf"))

        if not validate:
            return all_pdf_files

        # 验证文件
        print(f"正在验证 {len(all_pdf_files)} 个PDF文件...")
        valid_files = []
        invalid_files = []
        total_size_mb = 0
        estimated_total_pages = 0

        for pdf_file in all_pdf_files:
            validation = self.validate_file(pdf_file)
            if validation["valid"]:
                valid_files.append(pdf_file)
                total_size_mb += validation["size_mb"]

                # 估算页数
                estimated_pages = self.estimate_pdf_pages(pdf_file)
                estimated_total_pages += estimated_pages

                # 检查单个文件页数限制
                if estimated_pages > self.MAX_PAGES_PER_FILE:
                    invalid_files.append(
                        (pdf_file, f"估算页数 {estimated_pages} 可能超过限制 {self.MAX_PAGES_PER_FILE}页"))
                    print(f"PDF {pdf_file.name} 可能超过了系统限制已经被移除了队列")
                    valid_files.pop()  # 移除刚添加的文件
                    total_size_mb -= validation["size_mb"]
                    estimated_total_pages -= estimated_pages
            else:
                invalid_files.append((pdf_file, validation["reason"]))

        # 报告验证结果
        if invalid_files:
            print(f"发现 {len(invalid_files)} 个无效文件:")
            for file_path, reason in invalid_files:
                print(f"  - {file_path.name}: {reason}")

        if valid_files:
            print(f"✓ 找到 {len(valid_files)} 个有效PDF文件")
            print(f"  总大小: {total_size_mb:.1f}MB")
            print(f"  估算总页数: {estimated_total_pages} 页")

            # 日额度提醒
            if estimated_total_pages > self.DAILY_FREE_PAGES:
                print(f"  估算页数超过每日免费额度 {self.DAILY_FREE_PAGES} 页")
                print(f"   超出部分 ({estimated_total_pages - self.DAILY_FREE_PAGES} 页) 优先级将降低")

        return valid_files

    def request_upload_urls(self, pdf_files: List[Path], **options) -> Optional[Dict]:
        """
        申请文件上传链接

        Args:
            pdf_files: PDF文件路径列表
            **options: 解析选项

        Returns:
            包含batch_id和上传链接的字典
        """
        # 解析模型版本；API 文档：is_ocr / enable_* / language 对 pipeline 与 vlm 均有效
        model_version = str(options.get('model_version', 'pipeline'))
        m = model_version.lower()
        supports_vlm_pipeline_opts = m in ('pipeline', 'vlm')

        # 构建文件信息
        files_info = []
        for i, pdf_file in enumerate(pdf_files):
            # 生成安全的data_id，严格按照API要求：只能包含字母、数字、下划线、短划线、句号
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', pdf_file.stem)
            # 确保data_id不超过128个字符，并且有意义
            data_id = f"file_{i + 1:03d}_{safe_name}"[:128]

            file_info = {
                "name": pdf_file.name,
                "data_id": data_id
            }
            if supports_vlm_pipeline_opts:
                # 与历史 pipeline 行为及 config 模板一致；需关闭 OCR 时在 default_options 设 is_ocr: false
                file_info["is_ocr"] = bool(options.get('is_ocr', True))

            # 添加页面范围（如果指定）
            if 'page_ranges' in options and options['page_ranges']:
                file_info['page_ranges'] = str(options['page_ranges'])

            files_info.append(file_info)

        # 构建请求数据 - 严格按照API文档格式
        request_data = {
            "model_version": model_version,
            "files": files_info
        }
        if supports_vlm_pipeline_opts:
            request_data.update({
                "enable_formula": bool(options.get('enable_formula', True)),
                "enable_table": bool(options.get('enable_table', True)),
                "language": str(options.get('language', 'ch')),
            })

        # 添加额外格式（如果指定）
        extra_formats = options.get('extra_formats')
        if extra_formats and isinstance(extra_formats, list) and len(extra_formats) > 0:
            # 确保格式有效（支持 latex）
            valid_formats = ['html', 'docx', 'json', 'latex']
            filtered_formats = [fmt for fmt in extra_formats if fmt in valid_formats]
            if filtered_formats:
                request_data['extra_formats'] = filtered_formats

        # 添加回调（如果指定）
        if 'callback' in options and options['callback']:
            request_data['callback'] = str(options['callback'])
            request_data['seed'] = str(options.get('seed', 'mineru_batch_converter'))

        try:
            url = f"{self.base_url}/file-urls/batch"

            # 调试信息：显示请求数据（隐藏敏感信息）
            debug_data = request_data.copy()
            print(f"\n调试信息 - API请求:")
            print(f"URL: {url}")
            print(f"文件数量: {len(debug_data['files'])}")
            print(f"请求参数: {json.dumps(debug_data, ensure_ascii=False, indent=2)}")

            response = requests.post(url, headers=self.headers, json=request_data, timeout=30)

            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    self.upload_start_time = datetime.now()  # 记录申请时间
                    print(f"成功申请上传链接，batch_id: {result['data']['batch_id']}")
                    print(f"上传链接有效期: {self.UPLOAD_URL_VALID_HOURS}小时")
                    return result["data"]
                else:
                    error_code = str(result.get("code", "Unknown"))
                    error_msg = self.ERROR_CODES.get(error_code, result.get('msg', 'Unknown error'))
                    print(f"申请上传链接失败 [错误码: {error_code}]: {error_msg}")
                    return None
            else:
                print(f"✗ 请求失败，状态码: {response.status_code}")
                if response.text:
                    try:
                        error_info = response.json()
                        error_code = str(error_info.get("code", "Unknown"))
                        error_msg = self.ERROR_CODES.get(error_code, error_info.get('msg', 'Unknown error'))
                        print(f"   错误信息 [错误码: {error_code}]: {error_msg}")
                    except:
                        print(f"   响应内容: {response.text[:200]}")
                return None

        except requests.exceptions.Timeout:
            print(f"✗ 请求超时，请检查网络连接")
            return None
        except requests.exceptions.ConnectionError:
            print(f"✗ 网络连接错误，请检查网络状态")
            return None
        except Exception as e:
            print(f"✗ 申请上传链接时发生错误: {str(e)}")
            return None

    def check_upload_url_validity(self) -> bool:
        """
        检查上传链接是否仍然有效

        Returns:
            链接是否有效
        """
        if not self.upload_start_time:
            return True

        elapsed_time = datetime.now() - self.upload_start_time
        valid_duration = timedelta(hours=self.UPLOAD_URL_VALID_HOURS)

        if elapsed_time > valid_duration:
            print(f"上传链接已过期 (超过{self.UPLOAD_URL_VALID_HOURS}小时)")
            return False

        remaining_time = valid_duration - elapsed_time
        remaining_hours = remaining_time.total_seconds() / 3600
        print(f"上传链接剩余有效时间: {remaining_hours:.1f}小时")
        return True

    def upload_files(self, pdf_files: List[Path], upload_urls: List[str]) -> bool:
        """
        上传文件到指定的URL

        Args:
            pdf_files: PDF文件路径列表
            upload_urls: 上传URL列表

        Returns:
            是否全部上传成功
        """
        if len(pdf_files) != len(upload_urls):
            print("✗ 文件数量与上传链接数量不匹配")
            return False

        # 检查上传链接有效性
        if not self.check_upload_url_validity():
            return False

        success_count = 0
        failed_files = []

        for i, (pdf_file, upload_url) in enumerate(zip(pdf_files, upload_urls)):
            try:
                file_size_mb = pdf_file.stat().st_size / (1024 * 1024)
                print(f"正在上传 {pdf_file.name} ({file_size_mb:.1f}MB) [{i + 1}/{len(pdf_files)}]...")

                # 分块上传大文件
                with open(pdf_file, 'rb') as f:
                    upload_response = requests.put(
                        upload_url,
                        data=f,
                        timeout=self.upload_timeout,  # 使用配置的上传超时
                        stream=True
                    )

                if upload_response.status_code == 200:
                    print(f"✓ {pdf_file.name} 上传成功")
                    success_count += 1
                else:
                    error_msg = f"状态码: {upload_response.status_code}"
                    if upload_response.text:
                        error_msg += f", 响应: {upload_response.text[:100]}"
                    print(f"✗ {pdf_file.name} 上传失败，{error_msg}")
                    failed_files.append(pdf_file.name)

            except requests.exceptions.Timeout:
                print(f"✗ {pdf_file.name} 上传超时")
                failed_files.append(pdf_file.name)
            except requests.exceptions.ConnectionError:
                print(f"✗ {pdf_file.name} 网络连接错误")
                failed_files.append(pdf_file.name)
            except Exception as e:
                print(f"✗ 上传 {pdf_file.name} 时发生错误: {str(e)}")
                failed_files.append(pdf_file.name)

        print(f"\n上传完成: {success_count}/{len(pdf_files)} 个文件成功")
        if failed_files:
            print(f"失败文件: {', '.join(failed_files)}")

        return success_count == len(pdf_files)

    def wait_for_completion(self, batch_id: str, check_interval: int = 30, max_wait_time: int = 3600) -> List[Dict]:
        """
        等待批量任务完成

        Args:
            batch_id: 批量任务ID
            check_interval: 检查间隔（秒）
            max_wait_time: 最大等待时间（秒）

        Returns:
            完成的任务结果列表
        """
        print(f"开始监控批量任务 {batch_id}...")
        print(f"最大等待时间: {max_wait_time // 60} 分钟")

        start_time = time.time()
        retry_count = 0
        max_retries = 3
        last_status = {}

        while True:
            try:
                elapsed_time = time.time() - start_time
                if elapsed_time > max_wait_time:
                    print(f"等待超时 ({max_wait_time // 60} 分钟)")
                    return []

                url = f"{self.base_url}/extract-results/batch/{batch_id}"
                response = requests.get(url, headers=self.headers, timeout=30)

                if response.status_code == 200:
                    result = response.json()
                    if result.get("code") == 0:
                        data_obj = result.get("data", {})
                        extract_results = self._normalize_extract_results_data(data_obj)

                        if not extract_results:
                            print(f"\r等待任务列表就绪... ({int(elapsed_time)}s)", end='', flush=True)
                            time.sleep(check_interval)
                            continue

                        # 统计任务状态
                        status_count = defaultdict(int)
                        running_details = []

                        for task in extract_results:
                            state = task.get("state", "")
                            status_count[state] += 1

                            # 收集运行中任务的详细信息
                            if state == "running" and ("extract_progress" in task or "progress" in task):
                                progress = task.get("extract_progress") or task.get("progress") or {}
                                extracted = progress.get("extracted_pages", 0)
                                total = progress.get("total_pages", 0)
                                progress_pct = (extracted / total * 100) if total > 0 else 0
                                running_details.append(
                                    f"{task.get('file_name', '(unknown)')}: {extracted}/{total}页 ({progress_pct:.1f}%)")

                        # 显示状态变化
                        current_status = dict(status_count)
                        if current_status != last_status:
                            print(f"任务状态更新: {current_status}")
                            if running_details:
                                print("正在处理:")
                                for detail in running_details[:3]:  # 只显示前3个
                                    print(f"  • {detail}")
                                if len(running_details) > 3:
                                    print(f"  ... 还有 {len(running_details) - 3} 个任务正在处理")
                            last_status = current_status

                        # 检查是否所有任务都完成（含失败终态）；中间态见 API：waiting-file / pending / running / converting
                        all_done = all(task.get("state") in ("done", "failed") for task in extract_results)

                        if all_done:
                            successful_tasks = [task for task in extract_results if task.get("state") == "done"]
                            failed_tasks = [task for task in extract_results if task.get("state") == "failed"]

                            print(f"\n所有任务已完成!")
                            print(f"   成功: {len(successful_tasks)} 个")
                            print(f"   失败: {len(failed_tasks)} 个")
                            print(f"   耗时: {int(elapsed_time // 60)}分{int(elapsed_time % 60)}秒")

                            if failed_tasks:
                                print("\n失败的任务详情:")
                                for task in failed_tasks:
                                    error_msg = task.get('err_msg', 'Unknown error')
                                    print(f"  {task['file_name']}: {error_msg}")

                            return extract_results
                        else:
                            # 显示进度条
                            completed = status_count.get('done', 0) + status_count.get('failed', 0)
                            total_tasks = len(extract_results)
                            progress = completed / total_tasks * 100 if total_tasks > 0 else 0

                            progress_bar = '█' * int(progress // 5) + '░' * (20 - int(progress // 5))
                            print(
                                f"\r进度: [{progress_bar}] {progress:.1f}% ({completed}/{total_tasks}) | {int(elapsed_time // 60)}:{int(elapsed_time % 60):02d}",
                                end='')

                            time.sleep(check_interval)
                            retry_count = 0  # 重置重试计数
                    else:
                        error_code = str(result.get("code", "Unknown"))
                        error_msg = self.ERROR_CODES.get(error_code, result.get('msg', 'Unknown error'))
                        print(f"\n✗ 查询任务状态失败 [错误码: {error_code}]: {error_msg}")
                        return []
                else:
                    print(f"\n✗ 请求失败，状态码: {response.status_code}")
                    if response.text:
                        print(f"   响应: {response.text[:100]}")
                    return []

            except requests.exceptions.Timeout:
                retry_count += 1
                if retry_count > max_retries:
                    print(f"网络超时重试次数过多，停止监控")
                    return []
                print(f"网络超时，正在重试 ({retry_count}/{max_retries})...")
                time.sleep(10)
            except requests.exceptions.ConnectionError:
                retry_count += 1
                if retry_count > max_retries:
                    print(f"网络连接错误重试次数过多，停止监控")
                    return []
                print(f"网络连接错误，正在重试 ({retry_count}/{max_retries})...")
                time.sleep(10)
            except Exception as e:
                print(f"查询任务状态时发生错误: {str(e)}")
                time.sleep(check_interval)

    def _normalize_extract_results_data(self, data_obj: Dict) -> List[Dict]:
        """
        兼容 VLM 与 pipeline 的批量结果字段差异，将返回统一结构：
        [{file_name, state, zip_url, err_msg, extract_progress|progress, raw}]
        """
        # 可能的列表字段名
        candidates = [
            data_obj.get('extract_result'),
            data_obj.get('extract_results'),
            data_obj.get('results'),
            data_obj.get('tasks'),
            data_obj.get('items'),
        ]
        items = None
        for c in candidates:
            if isinstance(c, list):
                items = c
                break
        if items is None:
            return []

        normalized = []
        for it in items:
            if not isinstance(it, dict):
                continue
            file_name = it.get('file_name') or it.get('name') or it.get('filename')
            # 提取 zip URL 的多个可能字段
            zip_url = (
                    it.get('full_zip_url') or
                    it.get('zip_url') or
                    (it.get('outputs') or {}).get('full_zip_url') or
                    (it.get('result') or {}).get('full_zip_url')
            )
            state = it.get('state') or it.get('status')
            err_msg = it.get('err_msg') or it.get('error') or it.get('message')
            progress = it.get('extract_progress') or it.get('progress')
            normalized.append({
                'file_name': file_name,
                'zip_url': zip_url,
                'state': state,
                'err_msg': err_msg,
                'extract_progress': progress,
                'raw': it,
            })
        return normalized

    def download_and_extract_results(self, results: List[Dict], pdf_files: List[Path]) -> bool:
        """
        下载并解压结果到原位置

        Args:
            results: 任务结果列表
            pdf_files: 原始PDF文件路径列表

        Returns:
            是否全部处理成功
        """
        # 创建文件名到路径的映射
        file_mapping = {pdf_file.name: pdf_file for pdf_file in pdf_files}

        success_count = 0
        total_successful = len([r for r in results if r['state'] == 'done'])

        if total_successful == 0:
            print("没有成功处理的文件")
            return False

        print(f"开始下载 {total_successful} 个处理结果...")

        for i, result in enumerate(results):
            if result.get("state") != "done":
                continue

            file_name = result.get("file_name") or result.get("name") or ""
            zip_url = result.get("zip_url") or result.get("full_zip_url")

            if file_name not in file_mapping:
                print(f"✗ 找不到原始文件: {file_name}")
                continue

            # 保护：若API未返回下载链接，跳过并给出提示
            if not zip_url:
                print(f"✗ {file_name} 未返回 zip_url，可能任务尚未可下载或接口字段变化")
                continue

            original_file_path = file_mapping[file_name]

            try:
                current_num = success_count + 1
                print(f"正在下载 {file_name} [{current_num}/{total_successful}]...")
                # 创建临时文件保存ZIP
                temp_zip_path = original_file_path.parent / f"{original_file_path.stem}_temp.zip"
                # 具备断点续传和多次重试，缓解 CDN 的 TLS EOF 问题
                ok = self._download_file_with_resume(zip_url, temp_zip_path)
                if not ok:
                    print(f"✗ 下载失败: {file_name}")
                    # 清理可能的临时/部分ZIP
                    try:
                        if temp_zip_path.exists():
                            temp_zip_path.unlink()
                    except Exception:
                        pass
                    continue
                print(f"   ✓ 下载完成，正在解压...")

                # 解压到原文件所在目录
                extract_dir = original_file_path.parent / f"{original_file_path.stem}_mineru_output"

                # 如果目录存在，先清空
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                extract_dir.mkdir(parents=True, exist_ok=True)

                # 解压并统计文件
                extracted_files = []
                with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                    extracted_files = zip_ref.namelist()

                # 删除临时ZIP文件
                temp_zip_path.unlink()

                # 统计解压结果
                md_files = [f for f in extracted_files if f.endswith('.md')]
                json_files = [f for f in extracted_files if f.endswith('.json')]
                html_files = [f for f in extracted_files if f.endswith('.html')]
                docx_files = [f for f in extracted_files if f.endswith('.docx')]
                image_files = [f for f in extracted_files if f.lower().endswith(('.png', '.jpg', '.jpeg'))]

                print(f"{file_name} 处理完成!")
                print(f"输出目录: {extract_dir.name}")
                print(
                    f"文件统计: MD({len(md_files)}) JSON({len(json_files)}) HTML({len(html_files)}) DOCX({len(docx_files)}) 图片({len(image_files)})")

                success_count += 1

                # 七牛 CDN：无损压缩后上传，Markdown 改为居中 HTML（本地图片保留）
                process_extract_directory(extract_dir, self.qiniu)

            except zipfile.BadZipFile:
                print(f"✗ {file_name} 结果文件损坏")
                # 清理损坏的临时ZIP
                try:
                    if 'temp_zip_path' in locals() and temp_zip_path.exists():
                        temp_zip_path.unlink()
                except Exception:
                    pass
            except Exception as e:
                print(f"✗ 处理 {file_name} 时发生错误: {str(e)}")
                # 清理可能的临时文件
                temp_zip_path = original_file_path.parent / f"{original_file_path.stem}_temp.zip"
                if temp_zip_path.exists():
                    temp_zip_path.unlink()

        print(f"结果处理完成: {success_count}/{total_successful} 个文件成功")
        if success_count > 0:
            print(f"所有结果已保存到对应PDF文件同目录的 '*_mineru_output' 文件夹中")

        return success_count > 0

    def batch_convert(self, directory: str, recursive: bool = True, **options) -> bool:
        """
        批量转换指定目录下的PDF文件

        Args:
            directory: PDF文件所在目录
            recursive: 是否递归搜索子目录
            **options: 转换选项

        Returns:
            是否转换成功
        """
        print(f"开始批量转换 {directory} 目录下的PDF文件...")
        print(
            f"MinerU API 限制: 文件大小≤{self.MAX_FILE_SIZE_MB}MB, "
            f"页数≤{self.MAX_PAGES_PER_FILE}页, "
            f"单次申请上传链接≤{self.MAX_FILES_PER_FILE_URLS_BATCH}个（本脚本自动分批）"
        )

        # 1. 查找和验证PDF文件
        pdf_files = self.find_pdf_files(directory, recursive, validate=True)
        if not pdf_files:
            print("✗ 未找到有效的PDF文件")
            return False

        skip_existing_outputs = bool(options.pop('skip_existing_outputs', True))
        if skip_existing_outputs:
            skipped_files = []
            incomplete_files = []
            pending_files = []
            for pdf_file in pdf_files:
                extract_dir = pdf_file.parent / f"{pdf_file.stem}_mineru_output"
                if extract_dir.exists():
                    has_key_result = False
                    try:
                        has_key_result = any(extract_dir.rglob("*.md")) or any(extract_dir.rglob("*.json"))
                    except Exception:
                        has_key_result = False
                    if has_key_result:
                        skipped_files.append(pdf_file)
                    else:
                        incomplete_files.append(pdf_file)
                        pending_files.append(pdf_file)
                else:
                    pending_files.append(pdf_file)

            if skipped_files:
                print(f"检测到已有输出目录，已跳过 {len(skipped_files)} 个文件")

            if incomplete_files:
                print(f"检测到 {len(incomplete_files)} 个文件存在输出目录但缺少 .md/.json，视为未完成，将重新处理")

            pdf_files = pending_files
            if not pdf_files:
                print("所有PDF均已存在输出目录，无需重复处理")
                return True

        check_interval = int(options.get('check_interval', 30))
        max_wait_time = int(self.advanced.get('max_wait_time', 3600))
        chunk_size = self.file_urls_batch_size
        num_chunks = (len(pdf_files) + chunk_size - 1) // chunk_size

        overall_ok = True
        for chunk_idx in range(num_chunks):
            start = chunk_idx * chunk_size
            chunk_files = pdf_files[start:start + chunk_size]
            if num_chunks > 1:
                print(f"\n--- 上传分批 {chunk_idx + 1}/{num_chunks}（本批 {len(chunk_files)} 个文件）---")

            upload_data = self.request_upload_urls(chunk_files, **options)
            if not upload_data:
                overall_ok = False
                continue

            batch_id = upload_data["batch_id"]
            upload_urls = upload_data.get("file_urls") or upload_data.get("files")
            if not upload_urls:
                print("✗ 未从返回结果中解析到上传URL列表 (file_urls/files)")
                overall_ok = False
                continue

            if not self.upload_files(chunk_files, upload_urls):
                print("✗ 文件上传失败")
                overall_ok = False
                continue

            results = self.wait_for_completion(
                batch_id,
                check_interval=check_interval,
                max_wait_time=max_wait_time,
            )
            if not results:
                overall_ok = False
                continue

            if not self.download_and_extract_results(results, chunk_files):
                overall_ok = False

        return overall_ok

    def download_existing_batch(
            self,
            batch_id: str,
            directory: str,
            recursive: bool = True,
            check_interval: Optional[int] = None,
    ) -> bool:
        """
        仅根据已存在的 batch_id 下载结果，不重新上传文件。
        仍会在目录中查找原始 PDF 以建立 file_name->路径 的映射。
        """
        print(f"使用 batch_id={batch_id} 下载已有结果，不重新上传")
        # 查找目录中的PDF用于映射
        pdf_files = self.find_pdf_files(directory, recursive, validate=False)
        if not pdf_files:
            print("✗ 未在目录中找到任何 PDF 文件用于建立映射，请确认目录路径")
            return False
        poll = int(check_interval if check_interval is not None else self.advanced.get('check_interval', 30))
        max_wait_time = int(self.advanced.get('max_wait_time', 3600))
        # 等待/查询任务完成并下载
        results = self.wait_for_completion(
            batch_id,
            check_interval=poll,
            max_wait_time=max_wait_time,
        )
        if not results:
            return False
        return self.download_and_extract_results(results, pdf_files)


def main():
    """主函数 - 示例用法，从config.json读取配置"""
    load_project_dotenv()
    import json
    from pathlib import Path

    # 读取配置文件
    config_path = Path("config.json")

    if not config_path.exists():
        print("配置文件 config.json 不存在，请先运行 setup_wizard.py 创建配置")
        return False

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f"读取配置文件失败: {e}")
        return False

    # 从配置文件获取参数
    api_token = config.get('api_token')
    if not api_token or api_token == "请替换为您的MinerU API Token":
        print("请在 config.json 中设置有效的 API Token")
        return False

    # 获取目录设置
    directories = config.get('directories', {})
    pdf_directory = directories.get('default_pdf_directory', './pdfs')
    recursive_search = directories.get('recursive_search', True)

    # 获取转换选项（过滤掉注释键；check_interval 需参与轮询）
    default_options = config.get('default_options', {})
    convert_options = {
        key: value for key, value in default_options.items()
        if not key.startswith('_')
    }

    print(f"使用配置: API Token已设置, 目录: {pdf_directory}")
    print(f"转换选项: {convert_options}")

    # 创建转换器实例（传入advanced配置以启用更稳健的下载/上传参数）
    advanced_config = config.get('advanced', {})
    converter = MinerUBatchConverter(api_token, advanced=advanced_config, qiniu=config.get("qiniu"))

    try:
        # 执行批量转换
        success = converter.batch_convert(
            directory=pdf_directory,
            recursive=recursive_search,
            **convert_options
        )

        if success:
            print("\n批量转换完成!")
        else:
            print("\n批量转换失败!")

    except KeyboardInterrupt:
        print("\n用户中断操作")
    except Exception as e:
        print(f"\n发生错误: {str(e)}")


if __name__ == "__main__":
    main()
