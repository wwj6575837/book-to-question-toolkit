#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用版 Markdown 文件重组工具 (v3.2 - 篇章结构支持版)
修复了中文数字解析 Bug（如"第十一章"被错误解析为第1章）。
优化了特殊章节（如绪论）的命名显示。
新增支持"第X篇-第X章"层级结构（如中国美术简史）。
"""
import re
import argparse
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import quote
from hashlib import md5


class UniversalMarkdownReorganizer:
    def __init__(self):
        """初始化，定义正则表达式和映射关系。"""
        self.supported_extensions = {'.md', '.markdown'}

        # 核心修复：移除 '十'，将其严格视为单位，避免"十一"被解析为"1"
        self.cn_num_map = {
            '零': 0, '〇': 0, '○': 0, '一': 1, '二': 2, '三': 3, '四': 4,
            '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, 
            '两': 2
        }

        # 特殊前置章节映射
        self.special_chapters = {
            "序言": -3, "序": -3,
            "前言": -2,
            "引言": -1, "导论": -1,
            "绪论": 0, "导言": 0,
            "附录": 997,
        }

        # 正则表达式
        # 新增:专门识别"篇"的正则表达式
        self.re_part_section = re.compile(
            r'第\s*([一二三四五六七八九十百千\dIVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+)\s*篇'
        )
        self.re_chapter = re.compile(
            r'第\s*([一二三四五六七八九十百千\dIVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+)\s*(?:章|讲|编|课|单元)'
        )
        self.re_part_unit = re.compile(
            r'第\s*([一二三四五六七八九十百千\dIVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+)\s*(?:节|部分|课时)'
        )
        self.re_part_unit_en = re.compile(r'(?:Unit|Part|Section)\s*([0-9IVXLCDM]+)', re.IGNORECASE)
        self.re_part_numeric = re.compile(r'[_\-](\d+)(?!-\d+)(?![A-Za-z])')
        self.re_page_range = re.compile(r'(?<!\d)(\d+)\s*-\s*(\d+)(?!\d)')
        self.re_appendix = re.compile(r'附录\s*([A-Za-z])', re.IGNORECASE)

    def _looks_like_uuid(self, token: str) -> bool:
        if not token: return False
        if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', token):
            return True
        if '-' in token:
            hex_like = sum(c in '0123456789abcdefABCDEF-' for c in token)
            return hex_like / max(1, len(token)) > 0.8 and any(ch.isdigit() for ch in token)
        return False

    def _normalize_token(self, token: str) -> str:
        if not token: return token
        if token.endswith('_mineru_output'):
            token = token[:-len('_mineru_output')]
        token = token.replace('_mineru_output', '')
        return token

    def _filter_tokens(self, tokens: List[str]) -> List[str]:
        filtered = []
        for t in tokens:
            if not t: continue
            tl = t.lower()
            if tl in {'origin', 'images', 'image', 'img'}: continue
            if self._looks_like_uuid(t): continue
            filtered.append(t)
        return filtered

    def _extract_variant(self, tokens: List[str]) -> Optional[str]:
        if not tokens: return None
        variant_map = {'full': 'full', 'all': 'full', 'lite': 'lite', 'light': 'lite', '简化': 'lite', '精简': 'lite', '完整版': 'full'}
        cand = tokens[-1].lower()
        return variant_map.get(cand)

    def _cn_to_arabic(self, cn_num: str) -> int:
        """
        修复版中文数字转阿拉伯数字
        """
        if not cn_num: return -1
        s = cn_num.strip()
        
        # 1. 纯阿拉伯数字
        if s.isdigit(): return int(s)

        # 2. 罗马数字
        full2ascii = {
            'Ⅰ': 'I', 'Ⅱ': 'II', 'Ⅲ': 'III', 'Ⅳ': 'IV', 'Ⅴ': 'V',
            'Ⅵ': 'VI', 'Ⅶ': 'VII', 'Ⅷ': 'VIII', 'Ⅸ': 'IX', 'Ⅹ': 'X',
            'Ⅺ': 'XI', 'Ⅻ': 'XII'
        }
        roman = "".join([full2ascii.get(ch, ch) for ch in s]).upper()
        if all(c in 'IVXLCDM' for c in roman):
            values = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
            total, prev = 0, 0
            for ch in reversed(roman):
                val = values[ch]
                if val < prev: total -= val
                else: total += val
                prev = val
            if total > 0: return total

        # 3. 中文数字 (核心修复逻辑)
        unit_map = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
        
        total = 0
        section = 0
        number = 0
        have_num = False  # 标记当前段是否有数字

        for ch in s:
            if ch in self.cn_num_map:
                # 是数字（一、二...九）
                number = self.cn_num_map[ch]
                have_num = True
            elif ch in unit_map:
                # 是单位（十、百...）
                unit = unit_map[ch]
                if unit >= 10000:
                    # 万/亿结算
                    section = (section + number) * unit
                    total += section
                    section = 0
                    number = 0
                    have_num = False
                else:
                    # 十/百/千结算
                    if not have_num: 
                        # 处理 "十" 或 "十一" 开头的情况，默认前面是1
                        number = 1
                    section += number * unit
                    number = 0
                    have_num = False
            # 其他字符跳过

        # 加上最后剩下的数字
        result = total + section + number
        return result if result > 0 else -1

    def _find_markdown_files(self, book_root_dir: Path) -> List[Path]:
        markdown_files = []
        for ext in self.supported_extensions:
            markdown_files.extend(book_root_dir.rglob(f"*{ext}"))

        filtered_files = []
        for f in markdown_files:
            parts = f.parts
            is_in_output_dir = any(part.endswith("_mineru_output") for part in parts)
            is_in_origin_output_dir = any("origin_mineru_output" in part for part in parts)
            is_in_images_dir = "images" in parts
            is_in_organized_dir = any("organized" in part.lower() for part in parts)

            if is_in_output_dir and not is_in_origin_output_dir and not is_in_images_dir and not is_in_organized_dir:
                filtered_files.append(f)
        return filtered_files

    def _analyze_file_path(self, file_path: Path, book_root_dir: Path) -> Optional[Dict[str, Any]]:
        try:
            output_dir = next((p for p in file_path.parents if p.name.endswith("_mineru_output")), None)
            if not output_dir: return None

            info = {
                'original_path': file_path,
                'part_section_num': 0,  # 新增：篇的编号
                'part_section_name': "",  # 新增：篇的名称
                'chapter_num': 1000,
                'chapter_name': "",
                'part_num': 0,
                'page_start': 0,
                'page_end': 0,
            }

            raw_tokens = []
            cur = file_path
            while cur and cur != output_dir:
                raw_tokens.insert(0, cur.stem if cur.is_file() else cur.name)
                cur = cur.parent
            raw_tokens.insert(0, output_dir.name)

            norm_tokens = [self._normalize_token(t) for t in raw_tokens]
            tokens = self._filter_tokens(norm_tokens)
            full_name_str = "_".join(tokens)
            info['variant'] = self._extract_variant(tokens)

            # 新增：0. 识别"第X篇"结构
            for t in tokens:
                match = self.re_part_section.search(t)
                if match:
                    info['part_section_num'] = self._cn_to_arabic(match.group(1))
                    # 提取篇名称（篇号之后的内容）
                    raw_name = t[match.end():]
                    # 清理篇名：移除后续章节信息（如"第一章-先秦时期社会概况和文化"），避免重复
                    cleaned_name = raw_name.strip("-_ ")
                    # 使用章节正则查找第一个"第X章/单元/讲/编/课"并截断
                    m_chapter = self.re_chapter.search(cleaned_name)
                    if m_chapter:
                        cleaned_name = cleaned_name[:m_chapter.start()]
                    cleaned_name = cleaned_name.strip("-_ ")
                    info['part_section_name'] = cleaned_name
                    break

            # 1. 附录
            m_app = self.re_appendix.search(full_name_str)
            if m_app:
                letter = m_app.group(1).upper()
                info['chapter_num'] = 900 + (ord(letter) - ord('A'))
                info['chapter_name'] = f"附录{letter}"

            # 1.1 特殊章节(修正:精确匹配,避免子串匹配)
            if info['chapter_num'] == 1000:
                for name, num in self.special_chapters.items():
                    # 修正:只匹配独立的特殊章节名,避免匹配子串(例如"序"匹配到"秩序")
                    for t in tokens:
                        # 精确匹配:token等于特殊名,或者是token以特殊名开头/结尾(用分隔符分隔)
                        if t == name or t.startswith(name + ' ') or t.startswith(name + '_') or t.startswith(name + '-'):
                            info['chapter_num'] = num
                            info['chapter_name'] = name
                            break
                    if info['chapter_num'] != 1000:
                        break

            # 2. 第X章(修改:排除"篇",避免与篇号冲突)
            if info['chapter_num'] == 1000:
                for t in tokens:
                    match = self.re_chapter.search(t)
                    if match:
                        info['chapter_num'] = self._cn_to_arabic(match.group(1))
                        raw_name = t[match.end():]
                        # 清理章名：移除书名、篇名等冗余信息
                        cleaned_name = raw_name.replace(book_root_dir.name, '')
                        # 移除篇名信息（如果已经在part_section_name中）
                        if info.get('part_section_name'):
                            cleaned_name = cleaned_name.replace(info['part_section_name'], '')
                        info['chapter_name'] = cleaned_name.strip("-_ ")
                        break

            # 3. 页码范围
            if info['chapter_num'] == 1000:
                for t in tokens:
                    m_page = self.re_page_range.search(t)
                    if m_page:
                        info['page_start'] = int(m_page.group(1))
                        info['page_end'] = int(m_page.group(2))
                        info['chapter_num'] = info['page_start']
                        info['chapter_name'] = f"分段-{info['page_start']}-{info['page_end']}"
                        break
                else:
                    for t in tokens:
                        match = self.re_part_numeric.search(t)
                        if match:
                            info['chapter_num'] = int(match.group(1))
                            info['chapter_name'] = f"分段{match.group(0)}"
                            break

            # 4. 部分/单元
            part_num = 0
            for t in tokens:
                m_part = self.re_part_unit.search(t)
                if not m_part: m_part = self.re_part_unit_en.search(t)
                if m_part:
                    part_num = self._cn_to_arabic(m_part.group(1))
                    break
            if not part_num:
                for t in tokens:
                    m_num = self.re_part_numeric.search(t)
                    if m_num and info['chapter_num'] < 900:
                        cand = int(m_num.group(1))
                        if cand != info['chapter_num']:
                            part_num = cand
                            break
            if isinstance(part_num, int) and part_num > 0:
                info['part_num'] = part_num

            # 5. 回退
            if info['chapter_num'] == 1000:
                info['chapter_name'] = full_name_str.strip("-_ ")
                seed = int(md5(info['chapter_name'].encode('utf-8')).hexdigest()[:6], 16)
                info['chapter_num'] = 1000 + (seed % 1000)

            return info
        except Exception as e:
            print(f"分析失败 {file_path}: {e}")
            return None

    def _generate_filename(self, info: Dict[str, Any], book_title: str) -> str:
        clean_book_title = re.sub(r'\s+', '', book_title)
        clean_chapter_name = re.sub(r'[<>:"/\\|?*\s+]', '', info['chapter_name'])
        clean_chapter_name = clean_chapter_name.replace(clean_book_title, '').strip('-_')
        clean_part_section_name = re.sub(r'[<>:"/\\|?*\s+]', '', info.get('part_section_name', ''))
        variant = info.get('variant')

        # 新增:如果有"篇"信息,添加篇前缀
        part_section_prefix = ""
        if info.get('part_section_num', 0) > 0:
            part_section_prefix = f"第{info['part_section_num']}篇"
            if clean_part_section_name:
                part_section_prefix += f"-{clean_part_section_name}"
            part_section_prefix += "-"

        # 修复:绪论(0)等不需要加 "第00章" 前缀
        if info['chapter_num'] <= 0 or info['chapter_num'] >= 900:
            # 如果名字为空但有特殊编号(不太可能),给个默认
            name_part = clean_chapter_name if clean_chapter_name else "前言"
            base = f"{clean_book_title}-{part_section_prefix}{name_part}"
        else:
            # 普通章节
            base = f"{clean_book_title}-{part_section_prefix}第{info['chapter_num']}章"
            if clean_chapter_name and not clean_chapter_name.lower().startswith('分段'):
                base += f"-{clean_chapter_name}"

        part_seg = f"-部分{info['part_num']}" if info['part_num'] > 0 else ""
        if variant:
            return f"{base}_{variant}{part_seg}.md"
        return f"{base}{part_seg}.md"

    def _create_index_file(self, target_dir: Path, file_infos: List[Dict[str, Any]], book_title: str):
        index_content = [f"# {book_title} - 文件索引\n"]
        # 修改排序：先按篇，再按章，最后按部分
        file_infos.sort(key=lambda x: (x.get('part_section_num', 0), x['chapter_num'], x['part_num']))

        last_part_section_num = float('-inf')
        last_chapter_num = float('-inf')
        for info in file_infos:
            # 新增：篇章节标题
            if info.get('part_section_num', 0) > 0 and info['part_section_num'] != last_part_section_num:
                part_section_name = info.get('part_section_name', '')
                section_display = f": {part_section_name}" if part_section_name else ""
                index_content.append(f"\n# 第 {info['part_section_num']} 篇{section_display}")
                last_part_section_num = info['part_section_num']
                last_chapter_num = float('-inf')  # 重置章计数

            if info['chapter_num'] != last_chapter_num:
                if info['chapter_num'] <= 0 or info['chapter_num'] >= 900:
                    index_content.append(f"\n## {info['chapter_name']}")
                else:
                    chap_name_display = f": {info['chapter_name']}" if info['chapter_name'] and not info['chapter_name'].lower().startswith('分段') else ""
                    index_content.append(f"\n## 第 {info['chapter_num']} 章{chap_name_display}")
                last_chapter_num = info['chapter_num']

            new_filename = info.get('new_filename') or self._generate_filename(info, book_title)
            link = f"./{quote(new_filename)}"
            
            part_title = "主要内容"
            if info['part_num'] > 0: part_title = f"部分 {info['part_num']}"
            elif info['chapter_name'].lower().startswith('分段'): part_title = info['chapter_name']
            elif info['chapter_num'] <= 0 or info['chapter_num'] >= 900: part_title = info['chapter_name']

            index_content.append(f"- **{part_title}**: [{new_filename}]({link})")

        index_file = target_dir / "000-文件索引.md"
        index_file.write_text('\n'.join(index_content), encoding='utf-8')
        print(f"创建索引: {index_file.name}")

    def reorganize_book(self, source_dir: str, target_dir_base: str):
        source_path = Path(source_dir)
        book_title = source_path.name
        target_path = Path(target_dir_base) / f"{book_title}_organized"

        print(f"\n{'=' * 20}\n开始处理书籍: {book_title}")
        if target_path.exists():
            shutil.rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)

        all_files = self._find_markdown_files(source_path)
        if not all_files:
            print("未找到文件，请检查路径。")
            return

        file_infos = [info for f in all_files if (info := self._analyze_file_path(f, source_path)) is not None]
        # 修改排序：先按篇，再按章，最后按部分
        file_infos.sort(key=lambda x: (x.get('part_section_num', 0), x['chapter_num'], x['part_num']))

        seen_names = {}
        for info in file_infos:
            base_name = self._generate_filename(info, book_title)
            count = seen_names.get(base_name, 0)
            final_name = base_name
            if count > 0:
                base, ext = base_name.rsplit('.', 1)
                final_name = f"{base}-dup{count:02d}.{ext}"
            seen_names[base_name] = count + 1
            info['new_filename'] = final_name

        print("复制并重命名中...")
        for info in file_infos:
            target_file = target_path / info['new_filename']
            # 修改输出：显示篇章信息
            display_info = ""
            if info.get('part_section_num', 0) > 0:
                display_info = f"第{info['part_section_num']}篇-"
            display_info += f"第{info['chapter_num']}章"
            print(f"  {display_info} -> {info['new_filename']}")
            shutil.copy2(info['original_path'], target_file)

        self._create_index_file(target_path, file_infos, book_title)
        print(f"完成! 输出目录: {target_path.resolve()}")


if __name__ == "__main__":
    # --- 配置区域 ---
    DEFAULT_TARGET_BASE_DIR = r"D:\mz\MinerU\exports\Organized_Books"
    # 你的源目录路径
    DEFAULT_SOURCE_DIRS = [
        r"D:\desktop\普通高中教科书·通用技术必修 技术与设计1（已拆分）"
    ]

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--source", action="append")
    parser.add_argument("-t", "--target-base", default=DEFAULT_TARGET_BASE_DIR)
    args = parser.parse_args()

    target_base = args.target_base
    sources = args.source if args.source else DEFAULT_SOURCE_DIRS

    reorganizer = UniversalMarkdownReorganizer()
    for src_dir in sources:
        if Path(src_dir).exists():
            reorganizer.reorganize_book(src_dir, target_base)
        else:
            print(f"跳过不存在的目录: {src_dir}")