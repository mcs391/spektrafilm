"""Read-only thumbnail generation. QImage is safe to produce off the GUI thread."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from qtpy import QtCore, QtGui

from spektrafilm_gui.catalog import Photo, RAW_EXTENSIONS


THUMBNAIL_SIZE = 320


def thumbnail_path(photo: Photo, cache_dir: Path) -> Path:
    key = f'{photo.path}\0{photo.size}\0{photo.mtime_ns}\0{THUMBNAIL_SIZE}'
    return cache_dir / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.jpg')


def _read_image(reader: QtGui.QImageReader) -> QtGui.QImage:
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        size.scale(THUMBNAIL_SIZE, THUMBNAIL_SIZE, QtCore.Qt.KeepAspectRatio)
        reader.setScaledSize(size)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or '无法读取图像')
    return image


def make_thumbnail(photo: Photo, cache_dir: Path) -> tuple[QtGui.QImage, str]:
    """Return an image and optional diagnostic; never modify the input file."""
    target = thumbnail_path(photo, cache_dir)
    source = Path(photo.path)
    if source.is_file():
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (photo.size, photo.mtime_ns):
            return QtGui.QImage(), '原文件已变化，请重新导入以更新缩略图'
    if target.is_file():
        cached = QtGui.QImage(str(target))
        if not cached.isNull():
            return cached, ''
    if not source.is_file():
        return QtGui.QImage(), '原文件不可用；重新连接磁盘或重新定位文件'
    if source.suffix.lower() in RAW_EXTENSIONS:
        import rawpy
        with rawpy.imread(str(source)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                thumb = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                buffer = QtCore.QBuffer()
                buffer.setData(thumb.data)
                buffer.open(QtCore.QIODevice.ReadOnly)
                image = _read_image(QtGui.QImageReader(buffer))
            else:
                pixels = (thumb.data if thumb is not None else
                          raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8))
                height, width = pixels.shape[:2]
                image = QtGui.QImage(pixels.data, width, height, pixels.strides[0],
                                     QtGui.QImage.Format_RGB888).copy()
    else:
        image = _read_image(QtGui.QImageReader(str(source)))
    image = image.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, QtCore.Qt.KeepAspectRatio,
                         QtCore.Qt.SmoothTransformation)
    latest = source.stat()
    if (latest.st_size, latest.st_mtime_ns) != (photo.size, photo.mtime_ns):
        return QtGui.QImage(), '原文件在生成预览时发生变化，请重新导入'
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Concurrent thumbnail requests never expose partially written cache entries.
    fd, temporary = tempfile.mkstemp(prefix='.thumbnail-', suffix='.jpg', dir=cache_dir)
    os.close(fd)
    try:
        if not image.save(temporary, 'JPEG', 85):
            raise OSError('无法写入缩略图缓存')
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return image, ''
