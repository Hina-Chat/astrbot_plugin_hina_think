import os
import io
import tempfile
import traceback
import logging
from pathlib import Path
from typing import AsyncGenerator, Tuple, Dict, Any

import aiohttp
import qrcode
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import (
    SquareModuleDrawer,
    GappedSquareModuleDrawer,
    CircleModuleDrawer,
    RoundedModuleDrawer,
)
from qrcode.image.styles.colormasks import ImageColorMask
from PIL import Image

def _get_module_drawer(name: str = 'square'):
    """根據名稱獲取碼點繪製器實例。"""
    drawers = {
        "square": SquareModuleDrawer(),
        "gapped": GappedSquareModuleDrawer(),
        "circle": CircleModuleDrawer(),
        "rounded": RoundedModuleDrawer(),
    }
    return drawers.get(name.lower(), SquareModuleDrawer())

async def generate_qr_code(
    url: str, 
    qr_config: Dict[str, Any], 
    logger: logging.Logger, 
    storage_dir: Path, 
    temp_dir: Path
) -> AsyncGenerator[Tuple[str, str], None]:
    """根據配置生成個性化QR碼並異步返回結果。

    Yields:
        A tuple of (result_type, content), where result_type is 'image' or 'plain'.
    """
    qr_code_path = None
    embedded_logo_path = None
    try:
        # --- 讀取配置 ---
        box_size = qr_config.get('qr_box_size', 5)
        border = qr_config.get('qr_border', 2)
        module_drawer_name = qr_config.get('qr_module_drawer', 'square')
        image_mask_path_str = qr_config.get('qr_image_mask_path', '')
        logo_path_str = qr_config.get('qr_logo_path', '')

        logger.info(f"R1Filter: Generating custom QR code for URL: {url}")

        # --- 處理 Logo 和糾錯等級 ---
        error_correction = qrcode.constants.ERROR_CORRECT_M
        if logo_path_str:
            error_correction = qrcode.constants.ERROR_CORRECT_H
            logger.info("R1Filter: Logo detected, setting QR error correction to HIGH.")
            try:
                logo_content = None
                if logo_path_str.startswith(('http://', 'https://')):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(logo_path_str) as response:
                            if response.status == 200:
                                logo_content = await response.read()
                else:
                    logo_path = storage_dir / logo_path_str
                    if logo_path.exists():
                        with open(logo_path, 'rb') as f:
                            logo_content = f.read()
                
                if logo_content:
                    with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".png", delete=False) as temp_logo:
                        temp_logo.write(logo_content)
                        embedded_logo_path = temp_logo.name
                    logger.info(f"R1Filter: Logo saved to temporary file: {embedded_logo_path}")
                else:
                    logger.warning(f"R1Filter: Could not retrieve logo from: {logo_path_str}")

            except Exception as e:
                logger.error(f"R1Filter: Error processing logo: {e}")

        # --- 創建 QR Code 實例 ---
        qr = qrcode.QRCode(
            error_correction=error_correction,
            box_size=box_size,
            border=border,
        )
        qr.add_data(url)

        # --- 準備生成參數 ---
        make_image_kwargs = {
            'image_factory': StyledPilImage,
            'module_drawer': _get_module_drawer(module_drawer_name),
        }

        # --- 處理圖片蒙版 (支持 URL) ---
        if image_mask_path_str:
            try:
                image_content = None
                if image_mask_path_str.startswith(('http://', 'https://')):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(image_mask_path_str) as response:
                            if response.status == 200:
                                image_content = await response.read()
                else:
                    mask_path = storage_dir / image_mask_path_str
                    if mask_path.exists():
                        with open(mask_path, 'rb') as f:
                            image_content = f.read()

                if image_content:
                    mask_image = Image.open(io.BytesIO(image_content)).convert("RGBA")
                    make_image_kwargs['color_mask'] = ImageColorMask(
                        color_mask_image=mask_image, 
                        back_color=(255, 255, 255, 0)
                    )
                    logger.info("R1Filter: Successfully applied image mask.")
                else:
                    logger.warning(f"R1Filter: Could not retrieve image mask from: {image_mask_path_str}")
            except Exception as e:
                logger.error(f"R1Filter: Error processing image mask: {e}")

        # --- 嵌入 Logo ---
        if embedded_logo_path:
            make_image_kwargs['embedded_image_path'] = embedded_logo_path

        # --- 生成 QR Code 圖像並保存到臨時檔案 ---
        qr_img = qr.make_image(**make_image_kwargs)

        with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".png", delete=False) as temp_image:
            qr_img.save(temp_image, format='PNG')
            qr_code_path = temp_image.name
        
        logger.info(f"R1Filter: Custom QR code saved to {qr_code_path}")
        yield 'image', str(qr_code_path)

    except Exception as e:
        logger.error(f"R1Filter: Failed to generate custom QR code: {e}\n{traceback.format_exc()}")
        yield 'plain', f"生成分享 QR code 時出錯: {e}"
    finally:
        # 清理臨時檔案
        if qr_code_path and os.path.exists(qr_code_path):
            try:
                os.remove(qr_code_path)
            except OSError as e:
                logger.error(f"R1Filter: Error cleaning up QR code file {qr_code_path}: {e}")
        if embedded_logo_path and os.path.exists(embedded_logo_path):
            try:
                os.remove(embedded_logo_path)
            except OSError as e:
                logger.error(f"R1Filter: Error cleaning up logo file {embedded_logo_path}: {e}")
