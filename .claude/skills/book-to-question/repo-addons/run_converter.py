#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MinerU 批量转换器运行脚本
读取配置文件并执行批量转换
"""

import json
import argparse
from pathlib import Path
from mineru_batch_converter import MinerUBatchConverter
from project_dotenv import load_project_dotenv


def load_config(config_path: str = "config.json") -> dict:
    """加载配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"配置文件 {config_path} 不存在")
        return {}
    except json.JSONDecodeError:
        print(f"配置文件 {config_path} 格式错误")
        return {}


def validate_config(config: dict) -> bool:
    """验证配置文件的完整性"""
    required_keys = {
        'api_token': str,
        'directories': dict,
        'default_options': dict
    }

    for key, expected_type in required_keys.items():
        if key not in config:
            print(f"配置文件缺少必要的键: {key}")
            return False
        if not isinstance(config[key], expected_type):
            print(f"配置项 {key} 的类型不正确")
            return False

    # 检查directories子键欠缺
    dir_config = config['directories']
    if 'default_pdf_directory' not in dir_config:
        print("配置文件缺少 'directories.default_pdf_directory'")
        return False

    return True


def print_config_summary(config: dict):
    """打印配置摘要"""
    print("\n当前配置:")
    print("-" * 40)

    # 目录设置
    directories = config.get('directories', {})
    pdf_dir = directories.get('default_pdf_directory', '未设置')
    recursive = directories.get('recursive_search', True)

    # 处理可能的编码问题
    try:
        print(f"PDF目录: {pdf_dir}")
    except UnicodeEncodeError:
        # 如果路径包含特殊字符，显示简化版本
        safe_dir = pdf_dir.encode('ascii', 'ignore').decode('ascii') if pdf_dir else '未设置'
        print(f"PDF目录: {safe_dir} (包含特殊字符)")

    print(f"递归搜索: {'✅' if recursive else '❌'}")

    # 功能选项
    options = config.get('default_options', {})
    print(f"OCR识别: {'✅' if options.get('is_ocr', True) else '❌'}")
    print(f"公式识别: {'✅' if options.get('enable_formula', True) else '❌'}")
    print(f"表格识别: {'✅' if options.get('enable_table', True) else '❌'}")
    print(f"语言: {options.get('language', 'auto')}")
    print(f"模型: {options.get('model_version', 'v2')}")

    extra_formats = options.get('extra_formats', [])
    if extra_formats:
        print(f"额外格式: {', '.join(extra_formats)}")

    print("-" * 40)


def main():
    """主函数"""
    load_project_dotenv()
    print(" MinerU 批量PDF转换器")
    print("=" * 50)
    # CLI 参数
    parser = argparse.ArgumentParser(description="MinerU 批量转换器")
    parser.add_argument(
        "--download-only",
        dest="download_batch_id",
        help="仅根据已存在的 batch_id 下载结果，不重新上传"
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="自动确认，跳过交互式确认（默认已启用）"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="配置文件路径（默认 config.json）"
    )
    parser.add_argument(
        "--pdf-dir",
        help="覆盖 config.json 里的 directories.default_pdf_directory（不写回文件）"
    )
    args = parser.parse_args()

    # 默认行为等价于传入 -y，避免控制台卡在输入确认
    if not getattr(args, "yes", False):
        args.yes = True

    # 1. 加载和验证配置
    config = load_config(args.config)
    if not config:
        print("\n 提示: 运行 'python setup_wizard.py' 初始化配置")
        return

    if not validate_config(config):
        print("\n 请检查配置文件格式")
        return

    # 2. 检查API Token
    api_token = config.get("api_token", "").strip()
    if not api_token or api_token == "请替换为您的MinerU API Token":
        print(" API Token 未设置")
        print(" 请在 config.json 中设置正确的 API Token")
        print(" 获取Token: https://mineru.net")
        return

    # 3. 检查目录存在
    directories = config['directories']
    if args.pdf_dir:
        directories['default_pdf_directory'] = args.pdf_dir
    pdf_directory = directories['default_pdf_directory']
    pdf_path = Path(pdf_directory)

    if not pdf_path.exists():
        print(f" 目标目录不存在: {pdf_directory}")
        create_dir = 'y' if args.yes else input(f"是否创建目录? (Y/n): ").strip().lower()
        if create_dir not in ['n', 'no', '否']:
            pdf_path.mkdir(parents=True, exist_ok=True)
            print(f" 目录已创建: {pdf_path.absolute()}")
            print(" 请在此目录中放入PDF文件后重新运行")
            return
        else:
            return

    # 4. 显示配置摘要
    print_config_summary(config)

    # 5. 用户确认（下载-only 模式自动跳过开始转换确认）
    if not args.download_batch_id:
        print("\n就绪操作:")
        response = 'y' if args.yes else input("是否开始批量转换? (Y/n): ").strip().lower()
        if response in ['n', 'no', '否']:
            print("操作已取消")
            return

    # 6. 执行转换
    recursive_search = directories.get('recursive_search', True)
    default_options = config['default_options']

    # 过滤掉非功能配置项（check_interval 需传给 batch_convert 以控制轮询间隔）
    clean_options = {k: v for k, v in default_options.items()
                     if not k.startswith('_')}

    # 将高级配置传入以启用更稳健的下载/上传参数（重试、超时等）
    advanced_config = config.get('advanced', {})
    converter = MinerUBatchConverter(
        api_token,
        advanced=advanced_config,
        qiniu=config.get("qiniu"),
    )

    try:
        if args.download_batch_id:
            print("\n 开始仅下载已有批次结果...")
            poll_iv = int(default_options.get("check_interval", 30))
            success = converter.download_existing_batch(
                batch_id=args.download_batch_id,
                directory=str(pdf_directory),
                recursive=recursive_search,
                check_interval=poll_iv,
            )
        else:
            print("\n 开始批量转换...")
            success = converter.batch_convert(
                directory=str(pdf_directory),
                recursive=recursive_search,
                **clean_options
            )

        if success:
            if args.download_batch_id:
                print("\n 下载完成!")
            else:
                print("\n 批量转换完成!")
                print(" 结果已保存到各PDF文件同目录下的 '*_mineru_output' 文件夹中")
        else:
            if args.download_batch_id:
                print("\n 下载失败!")
            else:
                print("\n 批量转换失败!")
                print(" 请检查错误信息并重试")

    except KeyboardInterrupt:
        print("\n  用户中断操作")
    except Exception as e:
        print(f"\n 发生错误: {str(e)}")
        print(" 如果问题持续，请检查API Token和网络连接")


if __name__ == "__main__":
    main()
