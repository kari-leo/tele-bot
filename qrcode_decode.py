#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import base64
from pathlib import Path

import cv2
import numpy as np
import zxingcpp


def cv_imread_unicode(path: Path):
    """
    支持Windows中文路径
    """
    data = np.fromfile(str(path), dtype=np.uint8)

    if len(data) == 0:
        return None

    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def try_decode(img):
    """
    多种预处理方式尝试识别二维码
    """

    candidates = []

    candidates.append(img)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    candidates.append(gray)

    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        5,
    )
    candidates.append(binary)

    kernel = np.array(
        [
            [0, -1, 0],
            [-1, 5, -1],
            [0, -1, 0],
        ],
        dtype=np.float32,
    )

    sharp = cv2.filter2D(gray, -1, kernel)
    candidates.append(sharp)

    scales = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]

    for candidate in candidates:

        h, w = candidate.shape[:2]

        for scale in scales:

            resized = cv2.resize(
                candidate,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )

            try:
                result = zxingcpp.read_barcode(resized)

                if result is not None and result.text:
                    return result.text

            except Exception:
                pass

    return None


def decode_qr(image_path: Path):

    img = cv_imread_unicode(image_path)

    if img is None:
        raise RuntimeError(
            f"无法读取图片: {image_path}"
        )

    text = try_decode(img)

    if text is None:
        raise RuntimeError(
            f"未检测到二维码: {image_path}"
        )

    try:
        return json.loads(text)

    except Exception as e:
        raise RuntimeError(
            f"二维码JSON解析失败:\n"
            f"{image_path}\n{e}"
        )


def reconstruct_file(qr_dir: str, output_dir: str):

    qr_dir = Path(qr_dir)
    output_dir = Path(output_dir)

    if not qr_dir.exists():
        raise FileNotFoundError(
            f"目录不存在: {qr_dir}"
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    image_files = []

    image_files.extend(qr_dir.glob("*.jpg"))
    image_files.extend(qr_dir.glob("*.jpeg"))
    image_files.extend(qr_dir.glob("*.png"))

    image_files = sorted(image_files)

    if len(image_files) == 0:
        raise RuntimeError(
            f"未找到图片文件: {qr_dir}"
        )

    print(f"发现 {len(image_files)} 张二维码图片\n")

    fragments = {}

    filename = None
    total_count = None

    success_count = 0
    failed_files = []

    for img_path in image_files:

        try:

            info = decode_qr(img_path)

            f = info["f"]
            i = int(info["i"])
            n = int(info["n"])
            d = info["d"]

            if filename is None:
                filename = f

            elif filename != f:
                raise RuntimeError(
                    f"文件名不一致: {filename} != {f}"
                )

            if total_count is None:
                total_count = n

            elif total_count != n:
                raise RuntimeError(
                    "总分片数不一致"
                )

            fragments[i] = d

            success_count += 1

            print(
                f"[OK] 分片 {i + 1}/{n} "
                f"{img_path.name}"
            )

        except Exception as e:

            failed_files.append(img_path.name)

            print(
                f"[FAIL] {img_path.name}"
            )
            print(e)
            print()

    print("\n====================")
    print(f"成功: {success_count}")
    print(f"失败: {len(failed_files)}")
    print("====================\n")

    if total_count is None:
        raise RuntimeError(
            "没有成功识别任何二维码"
        )

    missing = [
        idx
        for idx in range(total_count)
        if idx not in fragments
    ]

    if missing:

        print("缺失分片:")
        print(missing)

        print("\n识别失败图片:")

        for f in failed_files:
            print(f)

        raise RuntimeError(
            "存在缺失分片，无法恢复文件"
        )

    print("开始拼接...")

    full_base64 = "".join(
        fragments[idx]
        for idx in range(total_count)
    )

    print("开始Base64解码...")

    try:

        file_bytes = base64.b64decode(
            full_base64,
            validate=True,
        )

    except Exception as e:

        raise RuntimeError(
            f"Base64解码失败:\n{e}"
        )

    output_file = output_dir / filename

    with open(output_file, "wb") as f:
        f.write(file_bytes)

    print("\n恢复成功")
    print(f"文件名: {filename}")
    print(f"输出路径: {output_file}")
    print(f"大小: {len(file_bytes):,} bytes")


if __name__ == "__main__":

    qr_image_dir = "/home/johnny/tele_bot/change_img"

    output_dir = "/home/johnny/tele_bot/change_assets"

    reconstruct_file(
        qr_dir=qr_image_dir,
        output_dir=output_dir,
    )
