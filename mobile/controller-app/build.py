#!/usr/bin/env python3
import os, subprocess, time

cwd = r'D:\Sources\hospital_helper\mobile\controller-app'
env = os.environ.copy()
env['JAVA_HOME'] = r'C:\Program Files\Java\jdk-21'
env['PATH'] = r'C:\Program Files\Java\jdk-21\bin' + os.pathsep + env.get('PATH', '')

t0 = time.time()
p = subprocess.Popen(
    [r'D:\Sources\hospital_helper\mobile\controller-app\gradlew.bat', 'assembleDebug', '--console=plain'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
)
out, err = p.communicate(timeout=900)
elapsed = time.time() - t0

def dec(b):
    return (b or b'').decode('utf-8', errors='replace')

with open(os.path.join(cwd, 'build_out.log'), 'w', encoding='utf-8') as f:
    f.write('=== STDOUT ===\n' + dec(out) + '\n=== STDERR ===\n' + dec(err) + '\n=== EXIT %d ===\n' % p.returncode)

text = dec(out)
lines = text.strip().splitlines()
print('\n'.join(lines[-80:]), flush=True)
print('EXIT:', p.returncode, 'ELAPSED: %.1fs' % elapsed, flush=True)
