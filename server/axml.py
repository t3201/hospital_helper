# -*- coding: utf-8 -*-
"""axml.py — 从 APK 的 AndroidManifest.xml（二进制 AXML）里读包名与版本。

用途：APK 上传时自动得到 versionCode / versionName / package，
      不必人工在命令行里重复填（手填值优先）。
实现：纯标准库（zipfile + struct），只解析到"根元素属性"这一层；解析失败一律返回 None
      （调用方回落到手填值/文件名推断），绝不抛异常打断上传。

AXML 结构参考 AOSP include/androidfw/ResourceTypes.h：
    文件 = RES_XML_TYPE(0x0003) 头 + 子块（字符串池 0x0001、资源映射 0x0180、
           START_NAMESPACE 0x0100 / START_ELEMENT 0x0102 / END_ELEMENT 0x0103 ...）
    Res_value.type: TYPE_STRING=0x03（data=字符串池下标） TYPE_INT_DEC=0x10（data=整数值）
"""
import struct
import zipfile

RES_XML = 0x0003
RES_STRING_POOL = 0x0001
RES_XML_START_ELEMENT = 0x0102
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
UTF8_FLAG = 1 << 8


def _decode_strings(chunk, count, flags):
    """chunk = 字符串池块（从块首开始）；-> list[str]。"""
    out = []
    offs = struct.unpack_from("<%dI" % count, chunk, 28)
    # ResStringPool_header: type2 hdr2 size4 strCount4 styleCount4 flags4 stringsStart4 stylesStart4
    strings_start = struct.unpack_from("<I", chunk, 20)[0]
    utf8 = bool(flags & UTF8_FLAG)
    for i in range(count):
        p = strings_start + offs[i]
        try:
            if utf8:
                # 先 UTF-16 字符数（1~2 字节，高位=1 表示 2 字节），再 UTF-8 字节数，再内容
                n = chunk[p]
                p += 1
                if n & 0x80:
                    p += 1
                m = chunk[p]
                p += 1
                if m & 0x80:
                    m = ((m & 0x7F) << 8) | chunk[p]
                    p += 1
                out.append(chunk[p:p + m].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", chunk, p)[0]
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", chunk, p)[0]
                    p += 2
                out.append(chunk[p:p + n * 2].decode("utf-16-le", "replace"))
        except Exception:
            out.append("")
    return out


def _iter_chunks(data, start, end):
    p = start
    while p + 8 <= end:
        rtype, hdr, size = struct.unpack_from("<HHI", data, p)
        if size <= 0 or p + size > end:
            break
        yield rtype, hdr, size, p
        p += size


def parse_manifest(manifest_bytes):
    """-> {'package':str|None,'version_code':int|None,'version_name':str|None}"""
    data = manifest_bytes
    res = {"package": None, "version_code": None, "version_name": None}
    if len(data) < 16:
        return res
    start = 0
    rtype, hdr, size = struct.unpack_from("<HHI", data, 0)
    if rtype == RES_XML:                       # 跳过外层 XML 头，进入子块
        start = hdr
    end = len(data)                            # 子块自带 size，逐个校验边界

    strings = []
    for rtype2, _h2, _s2, off in _iter_chunks(data, start, end):
        if rtype2 == RES_STRING_POOL:
            count, _styles, flags = struct.unpack_from("<III", data, off + 8)
            strings = _decode_strings(data[off:off + _s2], count, flags)
            break
    if not strings:
        return res

    for rtype2, h2, _s2, off in _iter_chunks(data, start, end):
        if rtype2 != RES_XML_START_ELEMENT:
            continue
        body = off + h2                        # ResXMLTree_attrExt 起点
        try:
            _ns, name_idx, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", data, body)
        except struct.error:
            return res
        ap = body + attr_start
        for _i in range(attr_count):
            try:
                _ans, aname, _araw, _vsize, _vres0, vtype, vdata = struct.unpack_from("<IIIHBBI", data, ap)
            except struct.error:
                break
            nm = strings[aname] if 0 <= aname < len(strings) else ""
            if nm == "package" and vtype == TYPE_STRING and vdata < len(strings):
                res["package"] = strings[vdata]
            elif nm == "versionCode" and vtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                res["version_code"] = int(vdata)
            elif nm == "versionName" and vtype == TYPE_STRING and vdata < len(strings):
                res["version_name"] = strings[vdata]
            ap += attr_size
        break                                  # 只看根 <manifest>
    return res


def read_apk_manifest(apk_path):
    """从 APK(zip) 里读 AndroidManifest.xml 并解析；任何异常 -> None。"""
    try:
        with zipfile.ZipFile(apk_path) as zf:
            names = [n for n in zf.namelist() if n.lower() == "androidmanifest.xml"]
            if not names:
                return None
            raw = zf.read(names[0])
        res = parse_manifest(raw)
        if res.get("version_code") is None and res.get("package") is None:
            return None
        return res
    except Exception:
        return None
