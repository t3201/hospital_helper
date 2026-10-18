# -*- coding: utf-8 -*-
"""轮换后验令牌: 旧口令必须 401, 新口令必须过鉴权(坏 body => 400 而非 401)。
只打印状态码与前缀, 不打印明文。"""
import os
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CTX = ssl._create_unverified_context()


def tok_from(path):
    if not os.path.isfile(path):
        return ''
    for line in open(path, encoding='utf-8', errors='replace'):
        if line.startswith('HH_ADMIN_TOKEN='):
            return line.split('=', 1)[1].strip()
    return ''


def post(tok):
    req = urllib.request.Request(
        'https://hh.ei.ac.cn:51120/v1/app/upload?file=x.apk',
        data=b'not-an-apk', method='POST',
        headers={'Content-Type': 'application/octet-stream', 'X-Admin-Token': tok})
    try:
        return urllib.request.urlopen(req, timeout=25, context=CTX).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return 'ERR:%s' % e


def main():
    old = tok_from(os.path.join(ROOT, 'server', '.env.bak'))
    new = tok_from(os.path.join(ROOT, 'server', '.env'))
    if not old or not new:
        print('!! 缺 .env.bak 或 .env, 无法比对')
        return 1
    if old == new:
        print('!! 新旧相同, 轮换没生效')
        return 1
    a, b = post(old), post(new)
    print('旧口令(%s…, %d 位) -> %s   期望 401' % (old[:2], len(old), a))
    print('新口令(%s…, %d 位) -> %s   期望 400(过鉴权, 坏 body)' % (new[:2], len(new), b))
    ok = (a == 401 and b == 400)
    print('令牌轮换校验: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
